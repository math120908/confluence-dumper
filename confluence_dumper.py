# -*- coding: utf-8 -*-

# confluence-dumper, a Python project to export spaces, pages and attachments
#
# Copyright (c) Siemens AG, 2016
#
# Authors:
#   Thomas Maier <thomas.tm.maier@siemens.com>
#
# This work is licensed under the terms of the MIT license.
# See the LICENSE.md file in the top-level directory.

"""
Confluence-dumper is a Python project to export spaces, pages and attachments
"""

from __future__ import print_function
import sys
import codecs

import os
import shutil
from six.moves import urllib_parse
from collections import namedtuple

from lxml import html
from lxml.etree import XMLSyntaxError

import utils
import settings

CONFLUENCE_DUMPER_VERSION = '1.0.0'
TITLE_OUTPUT = 'C O N F L U E N C E   D U M P E R  %s' % CONFLUENCE_DUMPER_VERSION


class ConfluenceClient(object):
    def __init__(self, base_url):
        self.base_url = base_url
        self.__cached_tinyurl = {}

    def get_page_details_by_page_id(self, page_id):
        response = self.request('/rest/api/content/%s?expand=children.page,children.attachment,body.view.value' % page_id)
        return response['title'], response['body']['view']['value']

    def get_homepage_info(self, space):
        response = self.request('/rest/api/space/%s?expand=homepage' % space)
        homepage_id = None
        if 'homepage' in response:
            homepage_id = response['homepage']['id']
        return response['name'], homepage_id

    def get_page_id_by_tinyurl(self, tinyurl):
        """ Convert from "/x/KuwCBw" to "/pages/viewpage.action?pageId=117632042" and extract id
        :param tinyurl:
        :return: corresponding page_id
        """
        hash_val = tinyurl.split('/')[-1]
        if hash_val in self.__cached_tinyurl:
            return self.__cached_tinyurl[hash_val]

        page_id = None
        for redirect_url in utils.http_get_iterate_redirect_url("{}/pages/tinyurl.action?urlIdentifier={}".format(self.base_url, hash_val)):
            if 'pageId' in redirect_url:
                queries = urllib_parse.parse_qs(urllib_parse.urlparse(redirect_url).query)
                page_id = int(queries['pageId'][0]) if 'pageId' in queries else None
                break

        if page_id:
            self.__cached_tinyurl[hash_val] = page_id
        return page_id

    def iterate_child_pages_by_page_id(self, page_id):
        for page in self.iterate_page('/rest/api/content/%s/child/page?limit=25' % page_id):
            yield page

    def iterate_attachments_by_page_id(self, page_id):
        for page in self.iterate_page('/rest/api/content/%s/child/attachment?limit=25' % page_id):
            yield page

    def iterate_spaces(self):
        for space in self.iterate_page('/rest/api/space?limit=25'):
            yield space

    def request(self, page_url):
        return utils.http_get(
            '%s%s' % (self.base_url, page_url),
            auth=settings.HTTP_AUTHENTICATION,
            headers=settings.HTTP_CUSTOM_HEADERS,
            verify_peer_certificate=settings.VERIFY_PEER_CERTIFICATE,
            proxies=settings.HTTP_PROXIES,
        )

    def iterate_page(self, start_page_url):
        page_url = start_page_url
        counter = 0
        while page_url:
            response = self.request(page_url)
            counter += len(response['results'])
            for result in response['results']:
                yield result

            if 'next' in response['_links'].keys():
                page_url = response['_links']['next']
                page_url = '%s%s' % (settings.CONFLUENCE_BASE_URL, page_url)
            else:
                page_url = None


PageInfo = namedtuple('PageInfo', ['file_path', 'page_title', 'child_pages', 'child_attachments'])

conf_client = ConfluenceClient(settings.CONFLUENCE_BASE_URL)


def error_print(*args, **kwargs):
    """ Wrapper for the print function which leads to stderr outputs.

    :param args: Not necessary.
    :param kwargs: Not necessary.
    """
    print(*args, file=sys.stderr, **kwargs)


def derive_downloaded_file_name(download_url):
    """ Generates the name of a downloaded/exported file.

        Example: /download/attachments/524291/peak.jpeg?version=1&modificationDate=1459521827579&api=v2
            => <download_folder>/524291_attachments_peak.jpeg
        Example: /download/thumbnails/524291/Harvey.jpg?version=1&modificationDate=1459521827579&api=v2
            => <download_folder>/524291_thumbnails_Harvey.jpg
        Example: /download/temp/plantuml14724856864114311137.png?contentType=image/png
            => <download_folder>/temp_plantuml14724856864114311137.png

    :param download_url: Confluence download URL which is used to derive the downloaded file name.
    :returns: Derived file name; if derivation is not possible, None is returned.
    """
    download_url = urllib_parse.urlparse(download_url).path  # Only retain path part
    if '/download/temp/' in download_url:
        return 'temp_%s' % download_url.split('/')[-1]

    elif '/download/' in download_url:
        download_url_parts = download_url.split('/')
        download_page_id = download_url_parts[3]
        download_file_type = download_url_parts[2]
        download_original_file_name = download_url_parts[-1]

        return '%s_%s_%s' % (download_page_id, download_file_type, download_original_file_name)
    elif '/rest/documentConversion/latest/conversion/thumbnail/' in download_url:
        file_id = download_url.split('/rest/documentConversion/latest/conversion/thumbnail/')[1][0:-2]
        return 'generated_preview_%s.jpg' % file_id
    else:
        return None


def provide_unique_file_name(duplicate_file_names, file_matching, file_title, is_folder=False,
                             explicit_file_extension=None):
    """ Provides an unique AND sanitized file name for a given page title. Confluence does not allow the same page title
    in one particular space but collisions are possible after filesystem sanitization.

    :param duplicate_file_names: A dict in the structure {'<sanitized filename>': amount of duplicates}
    :param file_matching: A dict in the structure {'<file title>': '<used offline filename>'}
    :param file_title: File title which is used to generate the unique file name
    :param is_folder: (optional) Flag which states whether the file is a folder
    :param explicit_file_extension: (optional) Explicitly set file extension (e.g. 'html')
    """
    if file_title in file_matching:
        file_name = file_matching[file_title]
    else:
        file_name = utils.sanitize_for_filename(file_title)

        if is_folder:
            file_extension = None
        elif explicit_file_extension:
            file_extension = explicit_file_extension
        else:
            if '.' in file_name:
                file_name, file_extension = file_name.rsplit('.', 1)
            else:
                file_extension = None

        if file_name in duplicate_file_names:
            duplicate_file_names[file_name] += 1
            file_name = '%s_%d' % (file_name, duplicate_file_names[file_name])
        else:
            duplicate_file_names[file_name] = 0
            file_name = file_name

        if file_extension:
            file_name += '.%s' % file_extension

        file_matching[file_title] = file_name
    return file_name


def parse_html_tree(html_content):
    if html_content == "":
        return None
    return html.fromstring(html_content)


def handle_html_references(html_tree, page_duplicate_file_names, page_file_matching, depth=0):
    """ Repairs links in the page contents with local links.

    :param html_tree: Confluence HTML tree.
    :param page_duplicate_file_names: A dict in the structure {'<sanitized filename>': amount of duplicates}
    :param page_file_matching: A dict in the structure {'<page title>': '<used offline filename>'}
    :param depth: (optional) Hierarchy depth of the handled Confluence page.
    :returns: Fixed HTML content.
    """
    if html_tree is None:
        return

    # Fix links to other Confluence pages
    # Example: /display/TES/pictest1
    #       => pictest1.html
    # TODO: This code does not work for "Recent space activity" areas in space pages because of a different url format.
    xpath_expr = '//a[contains(@href, "/display/")]'
    for link_element in html_tree.xpath(xpath_expr):
        if not link_element.get('class'):
            print("%s\033[1;30;40mLINK - %s\033[m" % ('\t' * (depth + 1), link_element.attrib['href']))
            path_name = urllib_parse.urlparse(link_element.attrib['href']).path
            page_title = path_name.split('/')[-1].replace('+', ' ')

            decoded_page_title = utils.decode_url(page_title)
            offline_link = provide_unique_file_name(page_duplicate_file_names, page_file_matching, decoded_page_title,
                                                    explicit_file_extension='html')
            link_element.attrib['href'] = utils.encode_url(offline_link)

    xpath_expr = '//a[contains(@href, "/x/")]'
    for link_element in html_tree.xpath(xpath_expr):
        if not link_element.get('class'):
            page_id = conf_client.get_page_id_by_tinyurl(link_element.attrib['href'])
            print("%s\033[1;30;40mLINK - %s (-> %s)\033[m" % ('\t' * (depth + 1), link_element.attrib['href'], page_id))
            if page_id:
                link_element.attrib['href'] = urllib_parse.urljoin(link_element.attrib['href'], "/pages/viewpage.action?pageId=%s" % page_id)

    # Fix links to other Confluence pages when page ids are used
    xpath_expr = '//a[contains(@href, "/pages/viewpage.action?pageId=")]'
    for link_element in html_tree.xpath(xpath_expr):
        if not link_element.get('class'):
            page_id = link_element.attrib['href'].split('/pages/viewpage.action?pageId=')[1]
            offline_link = '%s.html' % utils.sanitize_for_filename(page_id)
            link_element.attrib['href'] = utils.encode_url(offline_link)

    # Fix attachment links
    xpath_expr = '//a[contains(@class, "confluence-embedded-file")]'
    for link_element in html_tree.xpath(xpath_expr):
        file_url = link_element.attrib['href']
        file_name = derive_downloaded_file_name(file_url)
        relative_file_path = '%s/%s' % (settings.DOWNLOAD_SUB_FOLDER, file_name)
        # link_element.attrib['href'] = utils.encode_url(relative_file_path)
        link_element.attrib['href'] = relative_file_path

    # Fix file paths for img tags
    # TODO: Handle non-<img> tags as well if necessary.
    # TODO: Support files with different versions as well if necessary.
    possible_image_xpaths = ['//img[contains(@src, "/download/")]',
                             '//img[contains(@src, "/rest/documentConversion/latest/conversion/thumbnail/")]']
    xpath_expr = '|'.join(possible_image_xpaths)
    for img_element in html_tree.xpath(xpath_expr):
        # Replace file path
        file_url = img_element.attrib['src']
        file_name = derive_downloaded_file_name(file_url)
        relative_file_path = '%s/%s' % (settings.DOWNLOAD_SUB_FOLDER, file_name)
        img_element.attrib['src'] = relative_file_path

        # Add alt attribute if it does not exist yet
        if not 'alt' in img_element.attrib.keys():
            img_element.attrib['alt'] = relative_file_path

    return html.tostring(html_tree)


def download_file(clean_url, download_folder, downloaded_file_name, depth=0, error_output=True):
    """ Downloads a specific file.

    :param clean_url: Decoded URL to the file.
    :param download_folder: Folder to place the downloaded file in.
    :param downloaded_file_name: File name to save the download to.
    :param depth: (optional) Hierarchy depth of the handled Confluence page.
    :param error_output: (optional) Set to False if you do not want to see any error outputs
    :returns: Path to the downloaded file.
    """
    downloaded_file_path = '%s/%s' % (download_folder, downloaded_file_name)

    # Download file if it does not exist yet
    if not os.path.exists(downloaded_file_path):
        absolute_download_url = '%s%s' % (settings.CONFLUENCE_BASE_URL, clean_url)
        print('%sDOWNLOAD: %s' % ('\t' * (depth + 1), downloaded_file_name))
        try:
            utils.http_download_binary_file(absolute_download_url, downloaded_file_path,
                                            auth=settings.HTTP_AUTHENTICATION, headers=settings.HTTP_CUSTOM_HEADERS,
                                            verify_peer_certificate=settings.VERIFY_PEER_CERTIFICATE,
                                            proxies=settings.HTTP_PROXIES)

        except utils.ConfluenceException as e:
            if error_output:
                error_print('%sERROR: %s' % ('\t' * (depth + 2), e))
            else:
                print('%sWARNING: %s' % ('\t' * (depth + 2), e))

    return downloaded_file_path


def download_attachment(download_url, download_folder, attachment_id, attachment_duplicate_file_names,
                        attachment_file_matching, depth=0):
    """ Repairs links in the page contents with local links.

    :param download_url: Confluence download URL.
    :param download_folder: Folder to place downloaded files in.
    :param attachment_id: (optional) ID of the attachment to download.
    :param attachment_duplicate_file_names: A dict in the structure {'<sanitized attachment filename>': amount of \
                                            duplicates}
    :param attachment_file_matching: A dict in the structure {'<attachment title>': '<used offline filename>'}
    :param depth: (optional) Hierarchy depth of the handled Confluence page.
    :returns: Path and name of the downloaded file as dict.
    """
    clean_url = utils.decode_url(download_url)
    downloaded_file_name = derive_downloaded_file_name(clean_url)
    downloaded_file_name = provide_unique_file_name(attachment_duplicate_file_names, attachment_file_matching,
                                                    downloaded_file_name)
    downloaded_file_path = download_file(download_url, download_folder, downloaded_file_name, depth=depth)

    # Download the thumbnail as well if the attachment is an image
    clean_thumbnail_url = clean_url.replace('/attachments/', '/thumbnails/', 1)
    downloaded_thumbnail_file_name = derive_downloaded_file_name(clean_thumbnail_url)
    downloaded_thumbnail_file_name = provide_unique_file_name(attachment_duplicate_file_names, attachment_file_matching,
                                                              downloaded_thumbnail_file_name)
    if utils.is_file_format(downloaded_thumbnail_file_name, settings.CONFLUENCE_THUMBNAIL_FORMATS):
        # TODO: Confluence creates thumbnails always as PNGs but does not change the file extension to .png.
        download_file(clean_thumbnail_url, download_folder, downloaded_thumbnail_file_name, depth=depth,
                      error_output=False)

    # Download the image preview as well if Confluence generated one for the attachment
    if attachment_id is not None and utils.is_file_format(downloaded_file_name, settings.CONFLUENCE_GENERATED_PREVIEW_FORMATS):
        clean_preview_url = '/rest/documentConversion/latest/conversion/thumbnail/%s/1' % attachment_id
        downloaded_preview_file_name = derive_downloaded_file_name(clean_preview_url)
        downloaded_preview_file_name = provide_unique_file_name(attachment_duplicate_file_names,
                                                                attachment_file_matching, downloaded_preview_file_name)
        download_file(clean_preview_url, download_folder, downloaded_preview_file_name, depth=depth, error_output=False)

    return {'file_name': downloaded_file_name, 'file_path': downloaded_file_path}


def create_html_attachment_index(attachments):
    """ Creates a HTML list for a list of attachments.

    :param attachments: List of attachments.
    :returns: Attachment list as HTML.
    """
    html_content = '\n\n<h2>Attachments</h2>'
    if len(attachments) > 0:
        html_content += '<ul>\n'
        for attachment in attachments:
            relative_file_path = '/'.join(attachment['file_path'].split('/')[2:])
            relative_file_path = utils.encode_url(relative_file_path)
            html_content += '\t<li><a href="%s">%s</a></li>\n' % (relative_file_path, attachment['file_name'])
        html_content += '</ul>\n'
    return html_content


def download_attachments_of_page(page_id, download_folder, attachment_duplicate_file_names, attachment_file_matching, depth):
    child_attachments = []
    for attachment in conf_client.iterate_attachments_by_page_id(page_id):
        download_url = attachment['_links']['download']
        attachment_id = attachment['id'][3:]
        attachment_info = download_attachment(
            download_url,
            download_folder,
            attachment_id,
            attachment_duplicate_file_names,
            attachment_file_matching,
            depth=depth,
        )
        child_attachments.append(attachment_info)

    return child_attachments


def download_temp_attachments_of_page(page_tree, download_folder, attachment_duplicate_file_names, attachment_file_matching, depth):
    if page_tree is None:
        return []
    child_attachments = []
    for img_element in page_tree.xpath('//img[contains(@src, "/download/temp/")]'):
        file_url = img_element.attrib['src']
        attachment_info = download_attachment(
            file_url,
            download_folder,
            None,
            attachment_duplicate_file_names,
            attachment_file_matching,
            depth=depth,
        )
        child_attachments.append(attachment_info)
    return child_attachments


def fetch_page_recursively(page_id, folder_path, download_folder, html_template, depth=0,
                           page_duplicate_file_names=None, page_file_matching=None,
                           attachment_duplicate_file_names=None, attachment_file_matching=None):
    """ Fetches a Confluence page and its child pages (with referenced downloads).

    :param page_id: Confluence page id.
    :param folder_path: Folder to place downloaded pages in.
    :param download_folder: Folder to place downloaded files in.
    :param html_template: HTML template used to export Confluence pages.
    :param depth: (optional) Hierarchy depth of the handled Confluence page.
    :param page_duplicate_file_names: A dict in the structure {'<sanitized page filename>': amount of duplicates}
    :param page_file_matching: A dict in the structure {'<page title>': '<used offline filename>'}
    :param attachment_duplicate_file_names: A dict in the structure {'<sanitized attachment filename>': amount of \
                                            duplicates}
    :param attachment_file_matching: A dict in the structure {'<attachment title>': '<used offline filename>'}
    :rtype: PageInfo
    :returns: Information about downloaded files (pages, attachments, images, ...) as a PageInfo (None for exceptions)
    """
    if not page_duplicate_file_names:
        page_duplicate_file_names = {}
    if not page_file_matching:
        page_file_matching = {}
    if not attachment_duplicate_file_names:
        attachment_duplicate_file_names = {}
    if not attachment_file_matching:
        attachment_file_matching = {}

    try:
        page_title, page_content = conf_client.get_page_details_by_page_id(page_id)
        print('%sPAGE: %s (%s)' % ('\t' * (depth + 1), page_title, page_id))

        # Parse HTML
        html_tree = None
        try:
            html_tree = parse_html_tree(page_content)
        except XMLSyntaxError:
            print('%sWARNING: Could not parse HTML content of last page. Original content will be downloaded as it is.' % ('\t' * (depth + 1)))

        # Construct unique file name
        file_name = provide_unique_file_name(page_duplicate_file_names, page_file_matching, page_title, explicit_file_extension='html')

        # Remember this file and all children
        path_collection = PageInfo(file_path=file_name, page_title=page_title, child_pages=[], child_attachments=[])

        # Download attachments of this page
        path_collection.child_attachments.extend(download_attachments_of_page(
            page_id,
            download_folder,
            attachment_duplicate_file_names,
            attachment_file_matching,
            depth=depth + 1,
        ))
        path_collection.child_attachments.extend(download_temp_attachments_of_page(
            html_tree,
            download_folder,
            attachment_duplicate_file_names,
            attachment_file_matching,
            depth=depth + 1,
        ))
        # Export HTML file
        if html_tree is not None:
            page_content = handle_html_references(html_tree, page_duplicate_file_names, page_file_matching, depth + 1)
        file_path = '%s/%s' % (folder_path, file_name)
        page_content += create_html_attachment_index(path_collection.child_attachments)
        utils.write_html_2_file(file_path, page_title, page_content, html_template)

        # Save another file with page id which forwards to the original one
        id_file_path = '%s/%s.html' % (folder_path, page_id)
        id_file_page_title = 'Forward to page %s' % page_title
        original_file_link = utils.encode_url(utils.sanitize_for_filename(file_name))
        id_file_page_content = settings.HTML_FORWARD_MESSAGE % (original_file_link, page_title)
        id_file_forward_header = '<meta http-equiv="refresh" content="0; url=%s" />' % original_file_link
        utils.write_html_2_file(id_file_path, id_file_page_title, id_file_page_content, html_template,
                                additional_headers=[id_file_forward_header])

        # Iterate through all child pages
        for child_page in conf_client.iterate_child_pages_by_page_id(page_id):
            paths = fetch_page_recursively(child_page['id'], folder_path, download_folder, html_template,
                                           depth=depth + 1, page_duplicate_file_names=page_duplicate_file_names,
                                           page_file_matching=page_file_matching)
            if paths:
                path_collection.child_pages.append(paths)

        return path_collection

    except utils.ConfluenceException as e:
        error_print('%sERROR: %s' % ('\t' * (depth + 1), e))
        return None


def create_html_index(index_content):
    """ Creates an HTML index (mainly to navigate through the exported pages).

    :param PageInfo index_content: PageInfo contains file paths, page titles and their children recursively.
    :returns: Content index as HTML.
    """
    file_path = utils.encode_url(index_content.file_path)
    page_title = index_content.page_title
    page_children = index_content.child_pages

    html_content = '<a href="%s">%s</a>' % (utils.sanitize_for_filename(file_path), page_title)

    if len(page_children) > 0:
        html_content += '<ul>\n'
        for child in page_children:
            html_content += '\t<li>%s</li>\n' % create_html_index(child)
        html_content += '</ul>\n'

    return html_content


def print_welcome_output():
    """ Displays software title and some license information """
    print('\n\t %s' % TITLE_OUTPUT)
    print('\t %s\n' % ('=' * len(TITLE_OUTPUT)))
    print('... a Python project to export spaces, pages and attachments\n')
    print('Copyright (c) Siemens AG, 2016\n')
    print('Authors:')
    print('  Thomas Maier <thomas.tm.maier@siemens.com>\n')
    print('This work is licensed under the terms of the MIT license.')
    print('See the LICENSE.md file in the top-level directory.\n\n')


def print_finished_output():
    """ Displays exit message (for successful export) """
    print('\n\nFinished!\n')


def main():
    """ Main function to start the confluence-dumper. """

    # Configure console for unicode output via stdout/stderr
    # sys.stdout = codecs.getwriter('utf-8')(sys.stdout)
    # sys.stderr = codecs.getwriter('utf-8')(sys.stderr)

    # Welcome output
    print_welcome_output()
    # Delete old export
    if os.path.exists(settings.EXPORT_FOLDER):
        shutil.rmtree(settings.EXPORT_FOLDER)
    os.makedirs(settings.EXPORT_FOLDER)

    # Read HTML template
    template_file = open(settings.TEMPLATE_FILE)
    html_template = template_file.read()

    # Fetch all spaces if spaces were not configured via settings
    if len(settings.SPACES_PAGES_TO_EXPORT.keys()) > 0:
        spaces_pages_to_export = settings.SPACES_PAGES_TO_EXPORT
    else:
        spaces_pages_to_export = {space['key']: None for space in conf_client.iterate_spaces()}

    print('Exporting %d space(s): %s\n' % (len(spaces_pages_to_export), ', '.join(spaces_pages_to_export)))

    # Export spaces
    space_counter = 0
    duplicate_space_names = {}
    space_matching = {}
    for space, target_page_ids in spaces_pages_to_export.items():
        space_counter += 1

        # Init folders for this space
        space_folder_name = provide_unique_file_name(duplicate_space_names, space_matching, space, is_folder=True)
        space_folder = '%s/%s' % (settings.EXPORT_FOLDER, space_folder_name)
        os.makedirs(space_folder)
        download_folder = '%s/%s' % (space_folder, settings.DOWNLOAD_SUB_FOLDER)
        os.makedirs(download_folder)
        try:
            space_name, space_page_id = conf_client.get_homepage_info(space)
            print('SPACE (%d/%d): %s (%s)' % (space_counter, len(spaces_pages_to_export), space_name, space))

            if target_page_ids is None:
                target_page_ids = [space_page_id]

            index_page_info = PageInfo(file_path="", page_title="Index", child_pages=[], child_attachments=[])
            for target_page_id in target_page_ids:
                index_page_info.child_pages.append(fetch_page_recursively(target_page_id, space_folder, download_folder, html_template))

            if index_page_info:
                # Create index file for this space
                space_index_path = '%s/index.html' % space_folder
                space_index_title = 'Index of Space %s (%s)' % (space_name, space)
                space_index_content = create_html_index(index_page_info)
                utils.write_html_2_file(space_index_path, space_index_title, space_index_content, html_template)
        except utils.ConfluenceException as e:
            error_print('ERROR: %s' % e)
        except OSError:
            print('WARNING: The space %s has been exported already. Maybe you mentioned it twice in the settings'
                  % space)

    # Finished output
    print_finished_output()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        error_print('ERROR: Keyboard Interrupt.')
        sys.exit(1)
