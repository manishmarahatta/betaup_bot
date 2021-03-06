# -*- coding: utf-8 -*-
#
# This file is part of urlwatch (https://thp.io/2008/urlwatch/).
# Copyright (c) 2008-2016 Thomas Perl <thp.io/about>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
# 3. The name of the author may not be used to endorse or promote products
#    derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
# OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
# IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
# NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
# THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


import re
import logging
import itertools
import os
import imp
import html.parser
import hashlib

from enum import Enum

from .util import TrackSubClasses

logger = logging.getLogger(__name__)


class FilterBase(object, metaclass=TrackSubClasses):
    __subclasses__ = {}
    __anonymous_subclasses__ = []

    def __init__(self, job, state):
        self.job = job
        self.state = state

    def _no_subfilters(self, subfilter):
        if subfilter is not None:
            raise ValueError('No subfilters supported for {}'.format(self.__kind__))

    @classmethod
    def filter_documentation(cls):
        result = []
        for sc in TrackSubClasses.sorted_by_kind(cls):
            result.extend((
                '  * %s - %s' % (sc.__kind__, sc.__doc__),
            ))
        return '\n'.join(result)

    @classmethod
    def auto_process(cls, state, data):
        filters = itertools.chain((filtercls for _, filtercls in
                                   sorted(cls.__subclasses__.items(), key=lambda k_v: k_v[0])),
                                  cls.__anonymous_subclasses__)

        for filtercls in filters:
            filter_instance = filtercls(state.job, state)
            if filter_instance.match():
                logger.info('Auto-applying filter %r to %s', filter_instance, state.job.get_location())
                data = filter_instance.filter(data)

        return data

    @classmethod
    def process(cls, filter_kind, subfilter, state, data):
        logger.info('Applying filter %r, subfilter %r to %s', filter_kind, subfilter, state.job.get_location())
        filtercls = cls.__subclasses__.get(filter_kind, None)
        if filtercls is None:
            raise ValueError('Unknown filter kind: %s:%s' % (filter_kind, subfilter))
        return filtercls(state.job, state).filter(data, subfilter)

    def match(self):
        return False

    def filter(self, data, subfilter=None):
        raise NotImplementedError()


class AutoMatchFilter(FilterBase):
    """Automatically matches subclass filters with a given location"""
    MATCH = None

    def match(self):
        if self.MATCH is None:
            return False

        d = self.job.to_dict()
        result = all(d.get(k, None) == v for k, v in self.MATCH.items())
        logger.debug('Matching %r with %r result: %r', self, self.job, result)
        return result


class RegexMatchFilter(FilterBase):
    """Same as AutoMatchFilter but matching is done with regexes"""
    MATCH = None

    def match(self):
        if self.MATCH is None:
            return False

        d = self.job.to_dict()

        # It's a match if we have at least one key/value pair that matches,
        # and no key/value pairs that do not match
        matches = [v.match(d[k]) for k, v in self.MATCH.items() if k in d]
        result = len(matches) > 0 and all(matches)
        logger.debug('Matching %r with %r result: %r', self, self.job, result)
        return result


class LegacyHooksPyFilter(FilterBase):
    FILENAME = os.path.expanduser('~/.urlwatch/lib/hooks.py')

    def __init__(self, job, state):
        super().__init__(job, state)

        self.hooks = None
        if os.path.exists(self.FILENAME):
            try:
                self.hooks = imp.load_source('legacy_hooks', self.FILENAME)
            except Exception as e:
                logger.error('Could not load legacy hooks file: %s', e)

    def match(self):
        return self.hooks is not None

    def filter(self, data, subfilter=None):
        try:
            result = self.hooks.filter(self.job.get_location(), data)
            if result is None:
                result = data
            return result
        except Exception as e:
            logger.warn('Could not apply legacy hooks filter: %s', e)
            return data


class Html2TextFilter(FilterBase):
    """Convert HTML to plaintext"""

    __kind__ = 'html2text'

    def filter(self, data, subfilter=None):

        if subfilter is None:
            method = 're'
            options = {}
        elif isinstance(subfilter, dict):
            method = subfilter.pop('method')
            options = subfilter
        elif isinstance(subfilter, str):
            method = subfilter
            options = {}
        from .html2txt import html2text
        return html2text(data, method=method, options=options)


class Ical2TextFilter(FilterBase):
    """Convert iCalendar to plaintext"""

    __kind__ = 'ical2text'

    def filter(self, data, subfilter=None):
        self._no_subfilters(subfilter)
        from .ical2txt import ical2text
        return ical2text(data)


class GrepFilter(FilterBase):
    """Filter only lines matching a regular expression"""

    __kind__ = 'grep'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('The grep filter needs a regular expression')

        return '\n'.join(line for line in data.splitlines()
                         if re.search(subfilter, line) is not None)


class InverseGrepFilter(FilterBase):
    """Filter which removes lines matching a regular expression"""

    __kind__ = 'grepi'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('The inverse grep filter needs a regular expression')

        return '\n'.join(line for line in data.splitlines()
                         if re.search(subfilter, line) is None)


class StripFilter(FilterBase):
    """Strip leading and trailing whitespace"""

    __kind__ = 'strip'

    def filter(self, data, subfilter=None):
        self._no_subfilters(subfilter)
        return data.strip()


class FilterBy(Enum):
    ATTRIBUTE = 1
    TAG = 2


class ElementsBy(html.parser.HTMLParser):
    def __init__(self, filter_by, name, value=None):
        super().__init__()

        self._filter_by = filter_by
        if self._filter_by == FilterBy.ATTRIBUTE:
            self._attributes = {name: value}
        else:
            self._name = name

        self._result = []
        self._inside = False
        self._elts = []

    def get_html(self):
        return ''.join(self._result)

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)

        if self._filter_by == FilterBy.ATTRIBUTE and all(ad.get(k, None) == v for k, v in self._attributes.items()):
            self._inside = True
        elif self._filter_by == FilterBy.TAG and tag == self._name:
            self._inside = True

        if self._inside:
            self._result.append('<%s%s%s>' % (tag, ' ' if attrs else '',
                                              ' '.join('%s="%s"' % (k, v) for k, v in attrs)))
            self._elts.append(tag)

    def handle_endtag(self, tag):
        if self._inside:
            self._result.append('</%s>' % (tag,))
            if tag in self._elts:
                t = self._elts.pop()
                while t != tag and self._elts:
                    t = self._elts.pop()
            if not self._elts:
                self._inside = False

    def handle_data(self, data):
        if self._inside:
            self._result.append(data)


class GetElementById(FilterBase):
    """Get an HTML element by its ID"""

    __kind__ = 'element-by-id'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('Need an element ID for filtering')

        element_by_id = ElementsBy(FilterBy.ATTRIBUTE, 'id', subfilter)
        element_by_id.feed(data)
        return element_by_id.get_html()


class GetElementByClass(FilterBase):
    """Get all HTML elements by class"""

    __kind__ = 'element-by-class'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('Need an element class for filtering')

        element_by_class = ElementsBy(FilterBy.ATTRIBUTE, 'class', subfilter)
        element_by_class.feed(data)
        return element_by_class.get_html()


class GetElementByStyle(FilterBase):
    """Get all HTML elements by style"""

    __kind__ = 'element-by-style'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('Need an element style for filtering')

        element_by_style = ElementsBy(FilterBy.ATTRIBUTE, 'style', subfilter)
        element_by_style.feed(data)
        return element_by_style.get_html()


class GetElementByTag(FilterBase):
    """Get an HTML element by its tag"""

    __kind__ = 'element-by-tag'

    def filter(self, data, subfilter=None):
        if subfilter is None:
            raise ValueError('Need a tag for filtering')

        element_by_tag = ElementsBy(FilterBy.TAG, subfilter)
        element_by_tag.feed(data)
        return element_by_tag.get_html()


class Sha1Filter(FilterBase):
    """Calculate the SHA-1 checksum of the content"""

    __kind__ = 'sha1sum'

    def filter(self, data, subfilter=None):
        self._no_subfilters(subfilter)
        sha = hashlib.sha1()
        sha.update(data.encode('utf-8', 'ignore'))
        return sha.hexdigest()


class HexdumpFilter(FilterBase):
    """Convert binary data to hex dump format"""

    __kind__ = 'hexdump'

    def filter(self, data, subfilter=None):
        self._no_subfilters(subfilter)
        data = bytearray(data.encode('utf-8', 'ignore'))
        blocks = [data[i * 16:(i + 1) * 16] for i in range(int((len(data) + (16 - 1)) / 16))]
        return '\n'.join('%s  %s' % (' '.join('%02x' % c for c in block),
                                     ''.join((chr(c) if (c > 31 and c < 127) else '.')
                                             for c in block)) for block in blocks)
