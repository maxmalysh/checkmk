#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2019 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import sys
from typing import (
    Any,
    Callable,
    cast,
    Dict,
    Iterator,
    List,
    MutableMapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import cmk.utils.caching as caching
import cmk.utils.debug
from cmk.utils.check_utils import section_name_of
from cmk.utils.exceptions import MKParseFunctionError
from cmk.utils.type_defs import (
    CheckPluginNameStr,
    HostKey,
    ParsedSectionName,
    SectionName,
    SourceType,
)

from cmk.core_helpers.cache import ABCRawDataSection
from cmk.core_helpers.host_sections import HostSections

import cmk.base.api.agent_based.register as agent_based_register
import cmk.base.item_state as item_state
from cmk.base.api.agent_based.type_defs import SectionPlugin
from cmk.base.check_api_utils import HOST_PRECEDENCE as LEGACY_HOST_PRECEDENCE
from cmk.base.check_api_utils import MGMT_ONLY as LEGACY_MGMT_ONLY

ParsedSectionContent = Any


class ParsedSectionsBroker(MutableMapping[HostKey, HostSections]):
    """Object for aggregating, parsing and disributing the sections

    An instance of this class allocates all raw sections of a given host or cluster and
    hands over the parsed sections and caching information after considering features like
    'parsed_section_name' and 'supersedes' to all plugin functions that require this kind
    of data (inventory, discovery, checking, host_labels).
    """
    def __init__(self) -> None:
        super().__init__()
        self._data: Dict[HostKey, HostSections] = {}

        # This holds the result of the parsing of individual raw sections (by raw section name)
        self._memoized_parsing_results = caching.DictCache()
        # This holds the result of the superseding section along with the
        # cache info of the raw section that was used (by parsed section name!)
        self._memoized_parsed_sections = caching.DictCache()

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[HostKey]:
        return self._data.__iter__()

    def __getitem__(self, key: HostKey) -> HostSections:
        return self._data.__getitem__(key)

    def __setitem__(self, key: HostKey, value: HostSections) -> None:
        self._data.__setitem__(key, value)

    def __delitem__(self, key: HostKey) -> None:
        self._data.__delitem__(key)

    def __repr__(self) -> str:
        return "%s(data=%r)" % (type(self).__name__, self._data)

    # TODO (mo): consider making this a function
    def get_section_kwargs(
        self,
        host_key: HostKey,
        parsed_section_names: List[ParsedSectionName],
    ) -> Dict[str, Any]:
        """Prepares section keyword arguments for a non-cluster host

        It returns a dictionary containing one entry (may be None) for each
        of the required sections, or an empty dictionary if no data was found at all.
        """
        keys = (["section"] if len(parsed_section_names) == 1 else
                ["section_%s" % s for s in parsed_section_names])

        kwargs = {
            key: self.get_parsed_section(host_key, parsed_section_name)
            for key, parsed_section_name in zip(keys, parsed_section_names)
        }
        # empty it, if nothing was found:
        if all(v is None for v in kwargs.values()):
            kwargs.clear()

        return kwargs

    # TODO (mo): consider making this a function
    def get_section_cluster_kwargs(
        self,
        node_keys: List[HostKey],
        parsed_section_names: List[ParsedSectionName],
    ) -> Dict[str, Dict[str, Any]]:
        """Prepares section keyword arguments for a cluster host

        It returns a dictionary containing one optional dictionary[Host, ParsedSection]
        for each of the required sections, or an empty dictionary if no data was found at all.
        """
        kwargs: Dict[str, Dict[str, Any]] = {}
        for node_key in node_keys:
            node_kwargs = self.get_section_kwargs(node_key, parsed_section_names)
            for key, sections_node_data in node_kwargs.items():
                kwargs.setdefault(key, {})[node_key.hostname] = sections_node_data
        # empty it, if nothing was found:
        if all(v is None for s in kwargs.values() for v in s.values()):
            kwargs.clear()

        return kwargs

    def get_cache_info(
        self,
        parsed_section_names: List[ParsedSectionName],
    ) -> Optional[Tuple[int, int]]:
        """Aggregate information about the age of the data in the agent sections

        In order to determine the caching info for a parsed section we must in fact
        parse it, because otherwise we cannot know from which raw section to take
        the caching info.
        But fear not, the parsing itself is cached.
        """
        cache_infos = [
            cache_info  #
            for _parsed, cache_info in (  #
                self._get_parsed_section_with_cache_info(host_key, parsed_section_name)
                for host_key in self._data
                for parsed_section_name in parsed_section_names  #
            )  #
            if cache_info
        ]
        return (
            min(ats for ats, _intervals in cache_infos),
            max(intervals for _ats, intervals in cache_infos),
        ) if cache_infos else None

    def get_parsed_section(
        self,
        host_key: HostKey,
        parsed_section_name: ParsedSectionName,
    ) -> Optional[ParsedSectionContent]:
        return self._get_parsed_section_with_cache_info(host_key, parsed_section_name)[0]

    def _get_parsed_section_with_cache_info(
        self,
        host_key: HostKey,
        parsed_section_name: ParsedSectionName,
    ) -> Tuple[Optional[ParsedSectionContent], Optional[Tuple[int, int]]]:
        cache_key = host_key + (parsed_section_name,)
        if cache_key in self._memoized_parsed_sections:
            return self._memoized_parsed_sections[cache_key]

        try:
            host_sections = self._data[host_key]
        except KeyError:
            return self._memoized_parsed_sections.setdefault(cache_key, (None, None))

        for section in agent_based_register.get_ranked_sections(
                host_sections.sections,
            {parsed_section_name},
        ):
            parsed = self._get_parsing_result(host_key, section)
            if parsed is None:
                continue

            cache_info = host_sections.cache_info.get(section.name)
            return self._memoized_parsed_sections.setdefault(cache_key, (parsed, cache_info))

        return self._memoized_parsed_sections.setdefault(cache_key, (None, None))

    def determine_applicable_sections(
        self,
        parse_sections: Set[ParsedSectionName],
        source_type: SourceType,
    ) -> List[SectionPlugin]:
        """Try to parse all given sections and return a set of names for which the
        parsed sections value is not None.

        This takes into account the supersedings and permanently "dismisses" all
        superseded raw sections (by setting their parsing result to None).
        """
        applicable_sections: List[SectionPlugin] = []
        for host_key, host_sections in self._data.items():
            if host_key.source_type != source_type:
                continue

            for section in agent_based_register.get_ranked_sections(
                    host_sections.sections,
                    parse_sections,
            ):
                parsed = self._get_parsing_result(host_key, section)
                if parsed is None:
                    continue

                applicable_sections.append(section)
                self._memoized_parsed_sections[host_key + (section.parsed_section_name,)] = (
                    parsed,
                    host_sections.cache_info.get(section.name),
                )
                # set result of superseded ones to None:
                for superseded in section.supersedes:
                    self._memoized_parsing_results[host_key + (superseded,)] = None

        return applicable_sections

    def _get_parsing_result(
        self,
        host_key: HostKey,
        section: SectionPlugin,
    ) -> Any:
        # lookup the parsing result in the cache, it might have been computed
        # during resolving of the supersedings (or set to None b/c the section
        # *is* superseeded)
        cache_key = host_key + (section.name,)
        if cache_key in self._memoized_parsing_results:
            return self._memoized_parsing_results[cache_key]

        try:
            data = self._data[host_key].sections[section.name]
        except KeyError:
            return self._memoized_parsing_results.setdefault(cache_key, None)

        return self._memoized_parsing_results.setdefault(cache_key, section.parse_function(data))


# DEPRECATED
# This encapsulates the methods that are only required by legacy branches of "checking.py".
# Hopefully we can remove this class entirely someday.
class MultiHostSections:
    """Container object for wrapping the host sections of a host being processed
    or multiple hosts when a cluster is processed. Also holds the functionality for
    merging these information together for a check"""
    def __init__(self, parsed_sections_broker: ParsedSectionsBroker) -> None:
        super().__init__()
        self._parsed_sections_broker = parsed_sections_broker
        self._section_content_cache = caching.DictCache()

    def get_section_content(
        self,
        host_key: HostKey,
        management_board_info: str,
        check_plugin_name: CheckPluginNameStr,
        for_discovery: bool,
        *,
        cluster_node_keys: Optional[List[HostKey]] = None,
        check_legacy_info: Dict[str, Dict[str, Any]],
    ) -> Union[None, ParsedSectionContent, List[ParsedSectionContent]]:
        """Prepares the section_content construct for a Check_MK check on ANY host

        The section_content construct is then handed over to the check, inventory or
        discovery functions for doing their work.

        If the host is a cluster, the sections from all its nodes is merged together
        here. Optionally the node info is added to the nodes section content.

        It handles the whole data and cares about these aspects:

        a) Extract the section_content for the given check_plugin_name
        b) Adds node_info to the section_content (if check asks for this)
        c) Applies the parse function (if check has some)

        It can return an section_content construct or None when there is no section content
        for this check available.
        """

        section_name = section_name_of(check_plugin_name)
        cache_key = (host_key, management_board_info, section_name, for_discovery,
                     bool(cluster_node_keys))

        try:
            return self._section_content_cache[cache_key]
        except KeyError:
            pass

        section_content = self._get_section_content(
            host_key._replace(source_type=SourceType.MANAGEMENT if management_board_info ==
                              LEGACY_MGMT_ONLY else SourceType.HOST),
            check_plugin_name,
            SectionName(section_name),
            for_discovery,
            cluster_node_keys=cluster_node_keys,
            check_legacy_info=check_legacy_info,
        )

        # If we found nothing, see if we must check the management board:
        if (section_content is None and host_key.source_type is SourceType.HOST and
                management_board_info == LEGACY_HOST_PRECEDENCE):
            section_content = self._get_section_content(
                host_key._replace(source_type=SourceType.MANAGEMENT),
                check_plugin_name,
                SectionName(section_name),
                for_discovery,
                cluster_node_keys=cluster_node_keys,
                check_legacy_info=check_legacy_info,
            )

        self._section_content_cache[cache_key] = section_content
        return section_content

    def _get_section_content(
        self,
        host_key: HostKey,
        check_plugin_name: CheckPluginNameStr,
        section_name: SectionName,
        for_discovery: bool,
        *,
        cluster_node_keys: Optional[List[HostKey]] = None,
        check_legacy_info: Dict[str, Dict[str, Any]]
    ) -> Union[None, ParsedSectionContent, List[ParsedSectionContent]]:
        # Now get the section_content from the required hosts and merge them together to
        # a single section_content. For each host optionally add the node info.
        section_content: Optional[ABCRawDataSection] = None
        for node_key in cluster_node_keys or [host_key]:

            try:
                host_section_content = self._parsed_sections_broker[node_key].sections[section_name]
            except KeyError:
                continue

            if section_content is None:
                section_content = host_section_content[:]
            else:
                section_content += host_section_content

        if section_content is None:
            return None

        assert isinstance(section_content, list)

        return self._update_with_parse_function(
            section_content,
            section_name,
            check_legacy_info,
        )

    @staticmethod
    def _update_with_parse_function(
        section_content: ABCRawDataSection,
        section_name: SectionName,
        check_legacy_info: Dict[str, Dict[str, Any]],
    ) -> ParsedSectionContent:
        """Transform the section_content using the defined parse functions.

        Some checks define a parse function that is used to transform the section_content
        somehow. It is applied by this function.

        Please note that this is not a check/subcheck individual setting. This option is related
        to the agent section.

        All exceptions raised by the parse function will be catched and re-raised as
        MKParseFunctionError() exceptions."""
        # We can use the migrated section: we refuse to migrate sections with
        # "'node_info'=True", so the auto-migrated ones will keep working.
        # This function will never be called on checks programmed against the new
        # API (or migrated manually)
        if not agent_based_register.is_registered_section_plugin(section_name):
            # use legacy parse function for unmigrated sections
            parse_function = check_legacy_info.get(str(section_name), {}).get("parse_function")
        else:
            section_plugin = agent_based_register.get_section_plugin(section_name)
            parse_function = cast(Callable[[ABCRawDataSection], ParsedSectionContent],
                                  section_plugin.parse_function)

        if parse_function is None:
            return section_content

        # (mo): ValueStores (formally Item state) need to be *only* available
        # from within the check function, nowhere else.
        orig_item_state_prefix = item_state.get_item_state_prefix()
        try:
            item_state.set_item_state_prefix(section_name, None)
            return parse_function(section_content)

        except item_state.MKCounterWrapped:
            raise

        except Exception:
            if cmk.utils.debug.enabled():
                raise
            raise MKParseFunctionError(*sys.exc_info())

        finally:
            item_state.set_item_state_prefix(*orig_item_state_prefix)

    def legacy_determine_cache_info(self, section_name: SectionName) -> Optional[Tuple[int, int]]:
        """Aggregate information about the age of the data in the agent sections

        This is in checkers.g_agent_cache_info. For clusters we use the oldest
        of the timestamps, of course.
        """
        cache_infos = [
            host_sections.cache_info[section_name]
            for host_sections in self._parsed_sections_broker.values()
            if section_name in host_sections.cache_info
        ]

        return (min(at for at, _ in cache_infos),
                max(interval for _, interval in cache_infos)) if cache_infos else None
