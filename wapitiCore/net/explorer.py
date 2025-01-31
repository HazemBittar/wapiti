#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This file is part of the Wapiti project (https://wapiti-scanner.github.io)
# Copyright (C) 2022 Nicolas SURRIBAS
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
import asyncio
from collections import deque, defaultdict
import pickle
import math
from typing import Tuple, List, Optional, AsyncIterator
from urllib.parse import urlparse
import re

# Third-parties
import httpx

# Internal libraries
from wapitiCore.language.language import _
from wapitiCore.net import web

from wapitiCore.net.response import Response
from wapitiCore.net.web import Request, make_absolute
from wapitiCore.net.html import Html
from wapitiCore.main.log import logging, log_verbose
from wapitiCore.net.crawler import AsyncCrawler
from wapitiCore.net import swf
from wapitiCore.net import lamejs
from wapitiCore.net import jsparser_angular
from wapitiCore.net.scope import Scope


MIME_TEXT_TYPES = ('text/', 'application/xml')
# Limit page size to 2MB
MAX_PAGE_SIZE = 2097152

COMMON_PAGE_EXTENSIONS = {
    'php', 'html', 'htm', 'xml', 'xhtml', 'xht', 'xhtm', 'cgi',
    'asp', 'aspx', 'php3', 'php4', 'php5', 'txt', 'shtm',
    'shtml', 'phtm', 'phtml', 'jhtml', 'pl', 'jsp', 'cfm', 'cfml'
}

EXCLUDED_MEDIA_EXTENSIONS = (
    # File extensions we don't want to deal with. Js and SWF files won't be in this list.
    '.7z', '.aac', '.aiff', '.au', '.avi', '.bin', '.bmp', '.cab', '.dll', '.dmp', '.ear', '.exe', '.flv', '.gif',
    '.gz', '.ico', '.image', '.iso', '.jar', '.jpeg', '.jpg', '.mkv', '.mov', '.mp3', '.mp4', '.mpeg', '.mpg', '.pdf',
    '.png', '.ps', '.rar', '.scm', '.so', '.tar', '.tif', '.war', '.wav', '.wmv', '.zip'
)


BAD_URL_REGEX = re.compile(r"https?:/[^/]+")


def wildcard_translate(pattern):
    """Translate a wildcard PATTERN to a regular expression object.

    This is largely inspired by fnmatch.translate.
    """

    i, length = 0, len(pattern)
    res = ''
    while i < length:
        char = pattern[i]
        i += 1
        if char == '*':
            res += r'.*'
        else:
            res += re.escape(char)
    return re.compile(res + r'\Z(?ms)')


class Explorer:
    def __init__(self, crawler_instance: AsyncCrawler, scope: Scope, stop_event: asyncio.Event, parallelism: int = 8):
        self._crawler = crawler_instance
        self._scope = scope
        self._max_depth = 20
        self._max_page_size = MAX_PAGE_SIZE
        self._bad_params = set()
        self._max_per_depth = 0
        self._max_files_per_dir = 0
        self._qs_limit = 0
        self._hostnames = set()
        self._regexes = []
        self._processed_requests = []

        # Locking required for writing to the following structures
        self._file_counts = defaultdict(int)
        self._pattern_counts = defaultdict(int)
        self._custom_404_codes = {}
        # Corresponding lock
        self._shared_lock = asyncio.Lock()

        # Semaphore used to limit parallelism
        self._sem = asyncio.Semaphore(parallelism)
        # Event to stop processing tasks
        self._stopped = stop_event

        self._max_tasks = min(parallelism, 32)

    @property
    def max_depth(self) -> int:
        return self._max_depth

    @max_depth.setter
    def max_depth(self, value: int):
        self._max_depth = value

    @property
    def max_page_size(self) -> int:
        return self._max_page_size

    @max_page_size.setter
    def max_page_size(self, value: int):
        self._max_page_size = value

    @property
    def forbidden_parameters(self) -> set:
        return self._bad_params

    @forbidden_parameters.setter
    def forbidden_parameters(self, value: set):
        self._bad_params = value

    @property
    def max_requests_per_depth(self) -> int:
        return self._max_per_depth

    @max_requests_per_depth.setter
    def max_requests_per_depth(self, value: int):
        self._max_per_depth = value

    @property
    def max_files_per_dir(self) -> int:
        return self._max_files_per_dir

    @max_files_per_dir.setter
    def max_files_per_dir(self, value: int):
        self._max_files_per_dir = value

    @property
    def qs_limit(self) -> int:
        return self._qs_limit

    @qs_limit.setter
    def qs_limit(self, value: int):
        self._qs_limit = value

    def save_state(self, pickle_file: str):
        with open(pickle_file, "wb") as file_data:
            pickle.dump(
                {
                    "custom_404_codes": self._custom_404_codes,
                    "file_counts": self._file_counts,
                    "pattern_counts": self._pattern_counts,
                    "hostnames": self._hostnames
                },
                file_data,
                pickle.HIGHEST_PROTOCOL
            )

    def load_saved_state(self, pickle_file: str):
        try:
            with open(pickle_file, "rb") as file_data:
                data = pickle.load(file_data)
                self._custom_404_codes = data["custom_404_codes"]
                self._file_counts = data["file_counts"]
                self._pattern_counts = data["pattern_counts"]
                self._hostnames = data["hostnames"]
        except FileNotFoundError:
            pass

    def is_forbidden(self, candidate_url: str):
        return any(regex.match(candidate_url) for regex in self._regexes)

    def extract_links(self, response: Response, request) -> List:
        swf_links = []
        js_links = []
        allowed_links = []

        new_requests = []

        if "application/x-shockwave-flash" in response.type or request.file_ext == "swf":
            try:
                swf_links = swf.extract_links_from_swf(response.bytes)
            except Exception:  # pylint: disable=broad-except
                pass
        elif "/x-javascript" in response.type or "/x-js" in response.type or "/javascript" in response.type:
            js_links = lamejs.LameJs(response.content).get_links()
            js_links += jsparser_angular.JsParserAngular(response.url, response.content).get_links()

        elif response.type.startswith(MIME_TEXT_TYPES):
            html = Html(response.content, response.url)
            allowed_links.extend(self._scope.filter(html.links))
            allowed_links.extend(self._scope.filter(html.js_redirections + html.html_redirections))

            for extra_url in self._scope.filter(html.extra_urls):
                parts = urlparse(extra_url)
                # There are often css and js URLs with useless parameters like version or random number
                # used to prevent caching in browser. So let's exclude those extensions
                if parts.path.endswith(".css"):
                    continue

                if parts.path.endswith(".js") and parts.query:
                    # For JS script, allow to process them but remove parameters
                    allowed_links.append(extra_url.split("?")[0])
                    continue

                allowed_links.append(extra_url)

            for form in html.iter_forms():
                # TODO: apply bad_params filtering in form URLs
                if self._scope.check(form):
                    if form.hostname not in self._hostnames:
                        form.link_depth = 0
                    else:
                        form.link_depth = request.link_depth + 1

                    new_requests.append(form)

        for url in swf_links + js_links:
            if url:
                url = make_absolute(response.url, url)
                if url and self._scope.check(url):
                    allowed_links.append(url)

        for new_url in allowed_links:
            if "?" in new_url:
                path_only = new_url.split("?")[0]
                if path_only not in allowed_links and self._scope.check(path_only):
                    allowed_links.append(path_only)

        for new_url in set(allowed_links):
            if new_url == "":
                continue

            if self.is_forbidden(new_url):
                continue

            if "?" in new_url:
                path, query_string = new_url.split("?", 1)
                # TODO: encoding parameter ?
                get_params = [
                    list(t) for t in filter(
                        lambda param_tuple: param_tuple[0] not in self._bad_params,
                        web.parse_qsl(query_string)
                    )
                ]
            elif new_url.endswith(EXCLUDED_MEDIA_EXTENSIONS):
                # exclude static media files
                continue
            else:
                path = new_url
                get_params = []

            if response.is_directory_redirection and new_url == response.redirection_url:
                depth = request.link_depth
            else:
                depth = request.link_depth + 1

            new_requests.append(web.Request(path, get_params=get_params, link_depth=depth))

        return new_requests

    async def async_analyze(self, request) -> Tuple[bool, List, Optional[Response]]:
        async with self._sem:
            self._processed_requests.append(request)  # thread safe

            log_verbose(f"[+] {request}")

            dir_name = request.dir_name
            # Currently not exploited. Would be interesting though but then it should be implemented separately
            # Maybe in another task as we don't want to spend to much time in this function
            # async with self._shared_lock:
            #     # lock to prevent launching duplicates requests that would otherwise waste time
            #     if dir_name not in self._custom_404_codes:
            #         invalid_page = "zqxj{0}.html".format("".join([choice(ascii_letters) for __ in range(10)]))
            #         invalid_resource = web.Request(dir_name + invalid_page)
            #         try:
            #             page = await self._crawler.async_get(invalid_resource)
            #             self._custom_404_codes[dir_name] = page.status
            #         except httpx.RequestError:
            #             pass

            self._hostnames.add(request.hostname)

            resource_url = request.url

            try:
                response: Response = await self._crawler.async_send(request, stream=True)
            except (TypeError, UnicodeDecodeError) as exception:
                logging.debug(f"{exception} with url {resource_url}")  # debug
                return False, [], None
            except (ConnectionError, httpx.RequestError) as error:
                logging.error(_("[!] {} with URL {}").format(error.__class__.__name__, resource_url))
                return False, [], None

            if self._max_files_per_dir:
                async with self._shared_lock:
                    self._file_counts[dir_name] += 1

            if self._qs_limit and request.parameters_count:
                async with self._shared_lock:
                    self._pattern_counts[request.pattern] += 1

            # Above this line we need the content of the page. As we are in stream mode we must force reading the body.
            try:
                await response.read()
            finally:
                await response.close()

            if request.link_depth == self._max_depth:
                # We are at the edge of the depth so next links will have depth + 1 so to need to parse the page.
                return True, [], response

            # Sur les ressources statiques le content-length est généralement indiqué
            if self._max_page_size > 0:
                if response.raw_size > self._max_page_size:
                    return False, [], response

            await asyncio.sleep(0)
            resources = self.extract_links(response, request)
            # TODO: there's more situations where we would not want to attack the resource... must check this
            if response.is_directory_redirection:
                return False, resources, response

            return True, resources, response

    async def async_explore(
            self,
            to_explore: deque,
            excluded_urls: list = None
    ) -> AsyncIterator[Tuple[Request, Response]]:
        """Explore a single TLD or the whole Web starting with a URL

        @param to_explore: A list of Request of URLs (str) to scan the scan with.
        @type to_explore: list
        @param excluded_urls: A list of URLs to skip. Request objects or strings which may contain wildcards.
        @type excluded_urls: list

        @rtype: generator
        """
        if isinstance(excluded_urls, list):
            while True:
                try:
                    bad_request = excluded_urls.pop()
                except IndexError:
                    break
                else:
                    if isinstance(bad_request, str):
                        self._regexes.append(wildcard_translate(bad_request))
                    elif isinstance(bad_request, web.Request):
                        self._processed_requests.append(bad_request)

        if self._max_depth < 0:
            return

        task_to_request = {}
        while True:
            while to_explore:
                # Concurrent tasks are limited through the use of the semaphore BUT we don't want the to_explore
                # queue to be empty everytime (as we may need to extract remaining URLs) and overload the event loop
                # with pending tasks.
                if len(task_to_request) > self._max_tasks:
                    break

                if self._stopped.is_set():
                    break

                request = to_explore.popleft()
                if not isinstance(request, web.Request):
                    # We treat start_urls as if they are all valid URLs (ie in scope)
                    request = web.Request(request, link_depth=0)

                if request in self._processed_requests:
                    continue

                resource_url = request.url

                if request.link_depth > self._max_depth:
                    continue

                dir_name = request.dir_name
                if self._max_files_per_dir and self._file_counts[dir_name] >= self._max_files_per_dir:
                    continue

                # Won't enter if qs_limit is 0 (aka insane mode)
                if self._qs_limit:
                    if request.parameters_count:
                        try:
                            if self._pattern_counts[
                                request.pattern
                            ] >= 220 / (math.exp(request.parameters_count * self._qs_limit) ** 2):
                                continue
                        except OverflowError:
                            # Oh boy... that's not good to try to attack a form with more than 600 input fields
                            # but I guess insane mode can do it as it is insane
                            continue

                if self.is_forbidden(resource_url):
                    continue

                task = asyncio.create_task(self.async_analyze(request))
                task_to_request[task] = request

            if task_to_request:
                done, __ = await asyncio.wait(
                    task_to_request,
                    timeout=0.25,
                    return_when=asyncio.FIRST_COMPLETED
                )
            else:
                done = []

            # process any completed task
            for task in done:
                request = task_to_request[task]
                try:
                    success: bool
                    resources: List
                    response: Response
                    success, resources, response = await task
                except Exception as exception:    # pylint: disable=broad-except
                    logging.error(f"{request} generated an exception: {exception.__class__.__name__}")
                else:
                    if success:
                        yield request, response

                    accepted_urls = 0
                    for unfiltered_request in resources:
                        if BAD_URL_REGEX.search(unfiltered_request.file_path):
                            # Malformed link due to HTML issues
                            continue

                        if not self._scope.check(unfiltered_request):
                            continue

                        if unfiltered_request.hostname not in self._hostnames:
                            unfiltered_request.link_depth = 0

                        if unfiltered_request not in self._processed_requests and unfiltered_request not in to_explore:
                            to_explore.append(unfiltered_request)
                            accepted_urls += 1

                # remove the now completed task
                del task_to_request[task]

            if not task_to_request and (self._stopped.is_set() or not to_explore):
                break
