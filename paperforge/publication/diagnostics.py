from __future__ import annotations

import re
from collections.abc import Mapping

from .models import (
    CompileResult,
    LayoutDiagnosis,
    PublicationIssue,
    RenderResult,
    RepairContext,
    RepairProposal,
)
from .profiles import TemplateProfile


class DefaultLayoutDiagnostician:
    def __init__(self, *, page_limits: Mapping[str, int] | None = None) -> None:
        self.page_limits = dict(page_limits or {})

    def diagnose(
        self,
        compile_result: CompileResult,
        render_result: RenderResult | None,
        profile: TemplateProfile,
    ) -> LayoutDiagnosis:
        issues = list(compile_result.diagnostics)
        if compile_result.success:
            if render_result is None:
                issues.append(
                    PublicationIssue(
                        "RENDER_SKIPPED",
                        "successful compilation was not rendered for page inspection",
                        source="publication-engine",
                    )
                )
            else:
                issues.extend(render_result.diagnostics)
                limit = self.page_limits.get(profile.name, profile.page_limit)
                if (
                    render_result.success
                    and limit is not None
                    and render_result.page_count > limit
                ):
                    issues.append(
                        PublicationIssue(
                            "PAGE_LIMIT",
                            (
                                f"{profile.name} profile permits {limit} pages; "
                                f"rendered PDF has {render_result.page_count}"
                            ),
                            source="page-fit",
                            details={
                                "limit": limit,
                                "page_count": render_result.page_count,
                            },
                        )
                    )
        return LayoutDiagnosis(tuple(issues))


class ConstrainedLayoutRepairer:
    """Apply only reversible spacing changes; never rewrite manuscript content."""

    _FLOAT_SETTINGS = (
        (
            r"\setlength{\textfloatsep}{0.85\baselineskip}",
            r"\setlength{\floatsep}{0.75\baselineskip}",
        ),
        (
            r"\setlength{\textfloatsep}{0.70\baselineskip}",
            r"\setlength{\floatsep}{0.60\baselineskip}",
        ),
    )

    def repair(self, context: RepairContext) -> RepairProposal | None:
        codes = {issue.code for issue in context.diagnosis.issues if issue.blocking}
        if not codes.intersection({"OVERFLOW", "PAGE_FIT", "PAGE_LIMIT"}):
            return None

        index = min(max(context.round_number - 1, 0), len(self._FLOAT_SETTINGS) - 1)
        textfloatsep, floatsep = self._FLOAT_SETTINGS[index]
        additions = [textfloatsep, floatsep]
        if "OVERFLOW" in codes:
            additions.append(
                rf"\setlength{{\emergencystretch}}{{{context.round_number}em}}"
            )

        source = context.source_text
        for command in additions:
            length_match = re.match(r"\\setlength\{([^}]+)\}", command)
            assert length_match is not None
            length_name = re.escape(length_match.group(1))
            existing = re.compile(
                rf"\\setlength\s*\{{{length_name}\}}\s*\{{[^{{}}]*\}}"
            )
            if existing.search(source):
                source = existing.sub(command, source, count=1)
                continue
            begin_document = source.find(r"\begin{document}")
            if begin_document < 0:
                return None
            source = source[:begin_document] + command + "\n" + source[begin_document:]

        return RepairProposal(
            source_text=source,
            description=(
                f"round {context.round_number}: constrained float spacing"
                + (" and emergency stretch" if "OVERFLOW" in codes else "")
            ),
        )
