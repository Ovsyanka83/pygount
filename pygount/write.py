"""
Writers to store the results of a pygount analysis.
"""
# Copyright (c) 2016-2022, Thomas Aglassinger.
# All rights reserved. Distributed under the BSD License.
import datetime
import json
import math
import os
from xml.etree import ElementTree

from rich.console import Console
from rich.table import Table

import pygount

from . import SourceAnalysis
from .summary import ProjectSummary

#: Version of cloc the --format=cloc-xml pretends to be.
CLOC_VERSION = "1.60"

JSON_FORMAT_VERSION = "1.0.0"


class BaseWriter:
    def __init__(self, target_stream):
        self._target_stream = target_stream
        try:
            self.target_name = self._target_stream.name
        except AttributeError:
            self.target_name = "<io>"
        self.project_summary = ProjectSummary()
        self.started_at = datetime.datetime.utcnow()
        self.finished_at = None
        self.files_per_second = 0
        self.lines_per_second = 0
        self.duration = None
        self.duration_in_seconds = 0.0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def add(self, source_analysis):
        self.project_summary.add(source_analysis)

    def close(self):
        self.finished_at = datetime.datetime.utcnow()
        self.duration = self.finished_at - self.started_at
        self.duration_in_seconds = (
            (1e-6 * self.duration.microseconds) + self.duration.seconds + self.duration.days * 3600 * 24
        )
        if self.duration_in_seconds > 0:
            self.lines_per_second = self.project_summary.total_line_count / self.duration_in_seconds
            self.files_per_second = self.project_summary.total_file_count / self.duration_in_seconds


class LineWriter(BaseWriter):
    """
    Writer that simply writes a line of text for each source code.
    """

    def __init__(self, target_stream):
        super().__init__(target_stream)
        self.template = "{0}\t{1}\t{2}\t{3}"

    def add(self, source_analysis):
        source_line_count = source_analysis.code_count + source_analysis.string_count
        line_to_write = self.template.format(
            source_line_count, source_analysis.language, source_analysis.group, source_analysis.path
        )
        self._target_stream.write(line_to_write + os.linesep)


class ClocXmlWriter(BaseWriter):
    """
    Writer that writes XML output similar to cloc when called with options
    --by-file --xml. This kind of output can be processed by Jenkins' SLOCCount
    plug-in.
    """

    def __init__(self, target_stream):
        super().__init__(target_stream)
        self._results_element = ElementTree.Element("results")
        self._header_element = ElementTree.SubElement(self._results_element, "header")
        ElementTree.SubElement(self._header_element, "cloc_url", text="https://github.com/roskakori/pygount")
        ElementTree.SubElement(self._header_element, "cloc_version", text=CLOC_VERSION)
        self._files_element = ElementTree.SubElement(self._results_element, "files")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            # Only write the XML if everything works out.
            self.close()

    def add(self, source_analysis: SourceAnalysis):
        super().add(source_analysis)
        file_attributes = {
            "blank": str(source_analysis.empty_count),
            "code": str(source_analysis.source_count),
            "comment": str(source_analysis.documentation_count),
            "language": source_analysis.language,
            "name": source_analysis.path,
        }
        ElementTree.SubElement(self._files_element, "file", attrib=file_attributes)

    def close(self):
        super().close()
        # Add various statistics to <header>.
        ElementTree.SubElement(self._header_element, "elapsed_seconds", text="%.f" % self.duration_in_seconds)
        ElementTree.SubElement(self._header_element, "n_files", text="%d" % self.project_summary.total_file_count)
        ElementTree.SubElement(self._header_element, "n_lines", text="%d" % self.project_summary.total_line_count)
        ElementTree.SubElement(self._header_element, "files_per_second", text="%.f" % self.files_per_second)
        ElementTree.SubElement(self._header_element, "lines_per_second", text="%.f" % self.lines_per_second)
        ElementTree.SubElement(self._header_element, "report_file", text=self.target_name)

        # Add totals to <files>.
        file_attributes = {
            "blank": str(self.project_summary.total_empty_count),
            "code": str(self.project_summary.total_code_count + self.project_summary.total_string_count),
            "comment": str(self.project_summary.total_documentation_count),
        }
        ElementTree.SubElement(self._files_element, "total", attrib=file_attributes)

        # Write the whole XML file.
        if self._target_stream.encoding is not None:
            # Write XML declaration only for files but skip it for io.StringIO.
            self._target_stream.write('<?xml version="1.0" encoding="{0}"?>'.format(self._target_stream.encoding))
        xml_root = ElementTree.ElementTree(self._results_element)
        xml_root.write(self._target_stream, encoding="unicode", xml_declaration=False)

class SummaryWriter(BaseWriter):
    """
    Writer to summarize the analysis per language in a format that can easily
    be read by humans.
    """
    _TEXT_COLUMNS = "Language",
    _DIGIT_COLUMNS = "files", "blank", "comment", "code"
    def close(self):
        super().close()

        table = Table()

        for col in self._TEXT_COLUMNS:
            table.add_column(col)
        for col in self._DIGIT_COLUMNS:
            table.add_column(col, justify="right")
        language_summaries = sorted(self.project_summary.language_to_language_summary_map.values(), reverse=True)

        for index, language_summary in enumerate(language_summaries, start=1):
            table.add_row(
                language_summary.language,
                *map(
                    str,
                    (
                        language_summary.file_count,
                        language_summary.empty_count,
                        language_summary.documentation_count,
                        language_summary.code_count,
                    ),
                ),
                end_section=index == len(language_summaries),
            )
        table.add_row(
            "SUM",
            *map(
                str,
                (
                    self.project_summary.total_file_count,
                    self.project_summary.total_empty_count,
                    self.project_summary.total_documentation_count,
                    self.project_summary.total_code_count,
                ),
            ),
        )
        Console(file=self._target_stream).print(table)


class JsonWriter(BaseWriter):
    """
    Writer JSON output, ideal for further automatic processing.
    """

    def __init__(self, target_stream):
        super().__init__(target_stream)
        self.source_analyses = []

    def add(self, source_analysis: SourceAnalysis):
        super().add(source_analysis)
        self.source_analyses.append(
            {
                "path": source_analysis.path,
                "sourceCount": source_analysis.source_count,
                "emptyCount": source_analysis.empty_count,
                "documentationCount": source_analysis.documentation_count,
                "group": source_analysis.group,
                "isCountable": source_analysis.is_countable,
                "language": source_analysis.language,
                "state": source_analysis.state.name,
                "stateInfo": source_analysis.state_info,
            }
        )

    def close(self):
        # NOTE: We are using camel case for naming here to follow JSLint's guidelines, see <https://www.jslint.com/>.
        super().close()
        json_map = {
            "formatVersion": JSON_FORMAT_VERSION,
            "pygountVersion": pygount.__version__,
            "files": self.source_analyses,
            "languages": [
                {
                    "documentationCount": language_summary.documentation_count,
                    "emptyCount": language_summary.empty_count,
                    "fileCount": language_summary.file_count,
                    "isPseudoLanguage": language_summary.is_pseudo_language,
                    "language": language_summary.language,
                    "sourceCount": language_summary.source_count,
                }
                for language_summary in self.project_summary.language_to_language_summary_map.values()
            ],
            "runtime": {
                "durationInSeconds": self.duration_in_seconds,
                "filesPerSecond": self.files_per_second,
                "finishedAt": self.finished_at.isoformat(),
                "linesPerSecond": self.lines_per_second,
                "startedAt": self.started_at.isoformat(),
            },
            "summary": {
                "totalDocumentationCount": self.project_summary.total_documentation_count,
                "totalEmptyCount": self.project_summary.total_empty_count,
                "totalFileCount": self.project_summary.total_file_count,
                "totalSourceCount": self.project_summary.total_source_count,
            },
        }
        json.dump(json_map, self._target_stream)


def digit_width(line_count):
    assert line_count >= 0
    return math.ceil(math.log10(line_count + 1)) if line_count != 0 else 1
