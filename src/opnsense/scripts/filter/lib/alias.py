"""
    Copyright (c) 2017-2022 Ad Schellevis <ad@opnsense.org>
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
    OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.

    --------------------------------------------------------------------------------------
    Alias representation
"""
import os
import re
import time
import requests
import ipaddress
import dns.resolver
import syslog
import subprocess
from hashlib import md5
from dns.exception import DNSException
from . import geoip
from . import net_wildcard_iterator, AsyncDNSResolver
from .arpcache import ArpCache
from .interface import InterfaceParser

class Alias(object):
    def __init__(self, elem, known_aliases=[], ttl=-1, ssl_no_verify=False, timeout=120):
        """ construct alias object
            :param elem: ElementTree alias item
            :param known_aliases: all known alias names
            :param ttl: time to live in seconds (for other then ip/network types)
            :param ssl_no_verify: disable ssl verify when fetching content
            :param timeout: request timeout in seconds
            :return: None
        """
        self._known_aliases = known_aliases
        self._is_changed = None
        self._has_expired = None
        self._ttl = ttl
        self._ssl_no_verify = ssl_no_verify
        self._timeout = timeout
        self._name = None
        self._type = None
        self._interface = None
        self._proto = 'IPv4,IPv6'
        self._items = list()
        self._resolve_content = set()
        for subelem in elem:
            if subelem.tag == 'type':
                self._type = subelem.text
            elif subelem.tag == 'proto':
                self._proto = subelem.text
            elif subelem.tag == 'name':
                self._name = subelem.text
            elif subelem.tag == 'interface':
                self._interface = subelem.text
            elif subelem.tag == 'ttl':
                tmp = subelem.text.strip()
                if len(tmp.split('.')) <= 2 and tmp.replace('.', '').isdigit():
                    self._ttl = int(float(tmp))
            elif subelem.tag in ('aliasurl', 'address', 'url') and subelem.text is None:
                self._items = set()
            elif subelem.tag == 'aliasurl':
                self._items = set(sorted(subelem.text.split()))
            elif subelem.tag == 'address' and len(self._items) == 0:
                # special case, aliasurl fetched addresses in old implentation we don't want to use them
                self._items = set(sorted(subelem.text.split()))
            elif subelem.tag == 'url':
                self._items = set(sorted(subelem.text.split()))
        # we'll save the calculated hash for the unparsed alias content
        self._filename_alias_hash = '/var/db/aliastables/%s.md5.txt' % self._name
        # the generated alias contents, without dependencies
        self._filename_alias_content = '/var/db/aliastables/%s.self.txt' % self._name
        self._dnsResolver = AsyncDNSResolver(self._name)

    def _parse_address(self, address):
        """ parse addresses and hostnames, yield only valid addresses and networks
            :param address: address or network
            :return: boolean
        """
        address = address.strip()
        if address.find('/') > -1 and not address.split('/')[-1].isdigit():
            # wildcard netmask
            for idx, item in enumerate(net_wildcard_iterator(address.lstrip('!'))):
                if idx > 65535:
                    # overflow
                    syslog.syslog(syslog.LOG_ERR, 'alias table %s overflow' % self._name)
                    break
                yield "!%s" % item if address.startswith('!') else str(item)
        elif address.find('/') > -1:
            # provided address could be a network
            try:
                ipaddress.ip_network(str(address.lstrip('!')), strict=False)
                yield address
                return
            except (ipaddress.AddressValueError, ValueError):
                pass
        else:
            # check if address is an ipv4/6 address or range
            try:
                tmp = str(address).split('-')
                if len(tmp) > 1:
                    addr1 = ipaddress.ip_address(tmp[0])
                    # address range (from-to)
                    addr2 = ipaddress.ip_address(tmp[1])
                    for addr in ipaddress.summarize_address_range(addr1, addr2):
                        yield str(addr)
                else:
                    ipaddress.ip_address(tmp[0].lstrip('!'))
                    yield address
                return
            except (ipaddress.AddressValueError, ValueError):
                pass

        # try to resolve provided address (queue for retrieval)
        self._dnsResolver.add(address)

    def _fetch_url(self, url):
        """ return unparsed (raw) alias entries without dependencies
            :param url: url
            :return: iterator
        """
        # set request parameters
        req_opts = dict()
        req_opts['url'] = url
        req_opts['stream'] = True
        req_opts['timeout'] = self._timeout
        if self._ssl_no_verify:
            req_opts['verify'] = False
        # fetch data
        try:
            req = requests.get(**req_opts)
            if req.status_code == 200:
                # only handle content if response is correct
                req.raw.decode_content = True
                lines = req.raw.read().decode().splitlines()
                syslog.syslog(syslog.LOG_NOTICE, 'fetch alias url %s (lines: %s)' % (url, len(lines)))
                for line in lines:
                    raw_address = re.split(r'[\s,;|#]+', line)[0]
                    if raw_address and not raw_address.startswith('//'):
                        for address in self._parse_address(raw_address):
                            yield address
            else:
                syslog.syslog(syslog.LOG_ERR, 'error fetching alias url %s [http_code:%s]' % (url, req.status_code))
                raise IOError('error fetching alias url %s' % (url))
        except:
            syslog.syslog(syslog.LOG_ERR, 'error fetching alias url %s' % (url))
            raise IOError('error fetching alias url %s' % (url))

    def _fetch_geo(self, geoitem):
        """ fetch geoip addresses, if not downloaded or outdated force an update
            :return: iterator
        """
        do_update = True
        if os.path.isfile('/usr/local/share/GeoIP/alias/NL-IPv4'):
            fstat = os.stat('/usr/local/share/GeoIP/alias/NL-IPv4')
            if (time.time() - fstat.st_mtime) < (86400 - 90):
                do_update = False
        if do_update:
            syslog.syslog(syslog.LOG_ERR, 'geoip updated (files: %(file_count)d lines: %(address_count)d)' % geoip.download_geolite())

        for proto in self._proto.split(','):
            geoip_filename = "/usr/local/share/GeoIP/alias/%s-%s" % (geoitem, proto)
            if os.path.isfile(geoip_filename):
                with open(geoip_filename) as f_in:
                    for line in f_in:
                        for address in self._parse_address(line):
                            yield address

    def items(self):
        """ return unparsed (raw) alias entries without dependencies
            :return: iterator
        """
        for item in self._items:
            if item not in self._known_aliases or self.get_name() == item:
                yield item

    def uniqueid(self):
        """ generate an identification hash for this alias
            :return: md5 (string)
        """
        tmp = ','.join(sorted(list(self._items)))
        if self._proto:
            tmp = '%s[%s]' % (tmp, self._proto)
        return md5(tmp.encode()).hexdigest()

    def changed(self):
        """ is the alias changed (cached result, if changed within this objects lifetime)
            :return: boolean
        """
        if self._is_changed is None:
            if os.path.isfile(self._filename_alias_hash) and os.path.isfile(self._filename_alias_content):
                self._is_changed = open(self._filename_alias_hash).read().strip() != self.uniqueid()
            else:
                self._is_changed = True

        return self._is_changed

    def expired(self):
        """ if this alias has an expiry (ttl), has it reached the end of it's lifetime
            :return: boolean
        """
        if self._has_expired is None:
            if self._ttl > 0 and os.path.isfile(self._filename_alias_hash):
                fstat = os.stat(self._filename_alias_hash)
                self._has_expired = time.time() - fstat.st_mtime > self._ttl
            else:
                self._has_expired = False
        return self._has_expired

    def resolve(self, force=False):
        """ resolve (fetch) alias content, without dependencies.
            :force: force load
            :return: string
        """
        if not self._resolve_content:
            if self.expired() or self.changed() or force:
                if os.path.isfile(self._filename_alias_content):
                    try:
                        undo_content = open(self._filename_alias_content, 'r').read()
                    except UnicodeDecodeError:
                        undo_content = ""
                else:
                    undo_content = ""
                try:
                    self._resolve_content = self.pre_process()
                    address_parser = self.get_parser()
                    if address_parser:
                        for item in self.items():
                            for address in address_parser(item):
                                self._resolve_content.add(address)
                    # resolve hostnames (async) if there are any in the collected set
                    self._resolve_content = self._resolve_content.union(self._dnsResolver.collect().addresses())
                    with open(self._filename_alias_content, 'w') as f_out:
                        f_out.write('\n'.join(self._resolve_content))
                except (IOError, DNSException) as e:
                    syslog.syslog(syslog.LOG_ERR, 'alias resolve error %s (%s)' % (self._name, e))
                    # parse issue, keep data as-is, flush previous content to disk
                    with open(self._filename_alias_content, 'w') as f_out:
                        f_out.write(undo_content)
                    self._resolve_content = set(undo_content.split("\n"))
                # flush md5 hash to disk
                open(self._filename_alias_hash, 'w').write(self.uniqueid())
            else:
                self._resolve_content = set(open(self._filename_alias_content).read().split())
        # return the addresses and networks of this alias
        return list(self._resolve_content)

    def get_parser(self):
        """ fetch address parser to use, None if alias type is not handled here or only during pre processing
            :return: function or None
        """
        if self._type in ['host', 'network', 'networkgroup']:
            return self._parse_address
        elif self._type in ['url', 'urltable']:
            return self._fetch_url
        elif self._type == 'geoip':
            return self._fetch_geo
        elif self._type == 'dynipv6host':
            return InterfaceParser(self._interface).iter_dynipv6host
        elif self._type == 'mac':
            return ArpCache().iter_addresses
        else:
            return None

    def pre_process(self):
        """ alias type pre processors
            :return: set initial alias content
        """
        result = set()
        if self.get_type() == 'interface_net':
            subprocess.run(
                ['/sbin/pfctl', '-t', self.get_name(), '-T', 'replace', '%s:network' % self._interface],
                capture_output=True
            )
        # collect current table contents for selected types
        if self.get_type() in ['interface_net', 'external']:
            pfctl_cmd = ['/sbin/pfctl', '-t', self.get_name(), '-T', 'show']
            for line in subprocess.run(pfctl_cmd, capture_output=True, text=True).stdout.split('\n'):
                result.add(line.strip())

        return result

    def get_type(self):
        """ get type of alias
            :return: string
        """
        return self._type

    def get_name(self):
        """ get alias name
            :return: string
        """
        return self._name

    def get_deps(self):
        """ fetch alias dependencies
            :param in_data: raw input data (ruleset)
            :return: new ruleset
        """
        for item in self._items:
            if item in self._known_aliases:
                yield item
