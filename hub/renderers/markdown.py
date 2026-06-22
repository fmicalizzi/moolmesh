"""Markdown batch report renderer — runs all analyzers and outputs .md files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from hub.analyzers.base import BaseAnalyzer
from hub.models.base import UnifiedMessage

# Max size per file chunk (~2 MB)
_MAX_CHUNK_BYTES = 2 * 1024 * 1024


class MarkdownRenderer:
    """Renders analyzer results as individual Markdown files + a combined report."""

    def __init__(self, analyzers: list[BaseAnalyzer]) -> None:
        self.analyzers = sorted(analyzers, key=lambda a: a.name)

    def render_all(
        self,
        messages: list[UnifiedMessage],
        output_dir: Path,
        project_name: str = "all",
    ) -> list[Path]:
        """Run all analyzers and write Markdown files to output_dir.

        Large files (>2MB) are automatically split into numbered parts.
        Returns list of created file paths.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        # Clean previous report files to avoid stale artifacts
        for old_md in output_dir.glob("*.md"):
            old_md.unlink()
        created: list[Path] = []

        all_sections: list[str] = []
        all_sections.append(f"# MoolMesh — Report: {project_name}")
        all_sections.append(f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        all_sections.append(f"Messages analyzed: {len(messages)}\n")

        for analyzer in self.analyzers:
            results = analyzer.analyze(messages)
            md = analyzer.render_markdown(results)

            # Write individual file (split if too large)
            parts = self._write_split(output_dir, analyzer.name, md)
            created.extend(parts)

            all_sections.append(f"\n---\n\n{md}")

        # Combined report (also split if needed)
        combined_md = "\n".join(all_sections)
        combined_parts = self._write_split(output_dir, "00_full_report", combined_md)
        created = combined_parts + created

        return created

    @staticmethod
    def _write_split(output_dir: Path, base_name: str, content: str) -> list[Path]:
        """Write content to file, splitting into ~2MB parts if needed."""
        encoded = content.encode("utf-8")
        if len(encoded) <= _MAX_CHUNK_BYTES:
            fpath = output_dir / f"{base_name}.md"
            fpath.write_text(content, encoding="utf-8")
            return [fpath]

        # Split by lines, respecting section boundaries (--- separators)
        lines = content.split("\n")
        parts: list[Path] = []
        current_lines: list[str] = []
        current_size = 0
        part_num = 1

        for line in lines:
            line_size = len(line.encode("utf-8")) + 1  # +1 for newline
            if current_size + line_size > _MAX_CHUNK_BYTES and current_lines:
                # Write current chunk
                fpath = output_dir / f"{base_name}_part{part_num:02d}.md"
                chunk_text = "\n".join(current_lines)
                fpath.write_text(chunk_text, encoding="utf-8")
                parts.append(fpath)
                part_num += 1
                current_lines = [f"# {base_name} (parte {part_num})\n"]
                current_size = len(current_lines[0].encode("utf-8"))

            current_lines.append(line)
            current_size += line_size

        # Write last chunk
        if current_lines:
            fpath = output_dir / f"{base_name}_part{part_num:02d}.md"
            fpath.write_text("\n".join(current_lines), encoding="utf-8")
            parts.append(fpath)

        return parts
