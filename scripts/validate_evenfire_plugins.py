#!/usr/bin/env python3
"""Validate canonical Evenfire sources and the temporary generated distributions."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import build_evenfire_plugins as build


# Open-source hygiene: none of these may appear in shipped source or generated output.
FORBIDDEN_CONTENT = (
    "/Users/",
    "/home/",
    "marcotebalan",
    "palmeradao",
    "@palmeradao.xyz",
    "$HOME/",
)


class ValidationError(RuntimeError):
    """Raised when a validation contract is not satisfied."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    require(isinstance(value, dict), f"expected JSON object: {path}")
    return value


def strip_generated_marker(text: str) -> str:
    return text.replace(build.GENERATED_MARKER + "\n\n", "").replace(
        build.GENERATED_MARKER + "\n", ""
    )


def validate_json_schema(instance: Any, schema: dict[str, Any], location: str = "$") -> None:
    """Validate every JSON Schema feature used by the pinned 1.0.0 schema."""
    if "const" in schema:
        require(instance == schema["const"], f"{location} must equal {schema['const']!r}")
    expected_type = schema.get("type")
    if expected_type == "object":
        require(isinstance(instance, dict), f"{location} must be an object")
        properties = schema.get("properties", {})
        for required_name in schema.get("required", []):
            require(required_name in instance, f"{location}.{required_name} is required")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                validate_json_schema(value, properties[key], f"{location}.{key}")
            elif additional is False:
                raise ValidationError(f"{location} contains unknown field {key!r}")
            elif isinstance(additional, dict):
                validate_json_schema(value, additional, f"{location}.{key}")
    elif expected_type == "array":
        require(isinstance(instance, list), f"{location} must be an array")
        for index, value in enumerate(instance):
            validate_json_schema(value, schema.get("items", {}), f"{location}[{index}]")
    elif expected_type == "string":
        require(isinstance(instance, str), f"{location} must be a string")
        if "minLength" in schema:
            require(len(instance) >= schema["minLength"], f"{location} is too short")
        if "maxLength" in schema:
            require(len(instance) <= schema["maxLength"], f"{location} is too long")
        if "pattern" in schema:
            require(
                re.search(schema["pattern"], instance) is not None,
                f"{location} does not match {schema['pattern']!r}",
            )


def validate_no_symlinks_or_escapes(package: Path) -> None:
    require(not package.is_symlink(), f"generated package root is a symlink: {package}")
    require(package.is_dir(), f"generated package is not a directory: {package}")
    package_root = package.resolve()
    for path in build.iter_tree(package):
        require(
            build.is_within(path.resolve(strict=False), package_root),
            f"package path escapes root: {path}",
        )
        require(not path.is_symlink(), f"generated package contains a symlink: {path}")


def strip_code_spans(text: str) -> str:
    """Remove fenced and inline code so regex/code samples are not read as links."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`]*`", "", text)


def markdown_targets(markdown: Path) -> list[str]:
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    targets: list[str] = []
    for raw_target in pattern.findall(strip_code_spans(markdown.read_text(encoding="utf-8"))):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        targets.append(target.split("#", 1)[0])
    return targets


def validate_markdown_references(root: Path) -> None:
    for markdown in (path for path in build.iter_tree(root) if path.suffix == ".md"):
        for target in markdown_targets(markdown):
            resolved = (markdown.parent / target).resolve(strict=False)
            require(
                build.is_within(resolved, root),
                f"Markdown reference escapes package: {markdown}: {target}",
            )
            require(resolved.exists(), f"unresolved Markdown reference: {markdown}: {target}")


def validate_skill_reference_containment(skills_root: Path) -> None:
    """Every Markdown link in a source skill must resolve inside that skill's directory."""
    for skill_name in build.EXPECTED_SKILLS:
        skill_dir = (skills_root / skill_name).resolve()
        for markdown in (p for p in build.iter_tree(skills_root / skill_name) if p.suffix == ".md"):
            for target in markdown_targets(markdown):
                resolved = (markdown.parent / target).resolve(strict=False)
                require(
                    build.is_within(resolved, skill_dir),
                    f"skill Markdown reference escapes {skill_name}: {markdown}: {target}",
                )
                require(resolved.exists(), f"unresolved skill reference: {markdown}: {target}")


def scan_forbidden_content(root: Path) -> None:
    for path in (p for p in build.iter_tree(root) if p.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in FORBIDDEN_CONTENT:
            require(needle not in text, f"forbidden content {needle!r} in {path}")


def validate_skill_set(skills_root: Path) -> None:
    names = tuple(sorted(path.name for path in skills_root.iterdir() if path.is_dir()))
    require(names == build.EXPECTED_SKILLS, f"expected {build.EXPECTED_SKILLS} in {skills_root}: {names}")
    frontmatter_names: list[str] = []
    for skill_name in names:
        skill_file = skills_root / skill_name / "SKILL.md"
        require(skill_file.is_file(), f"missing SKILL.md in {skills_root / skill_name}")
        name, description = build.parse_skill_frontmatter(skill_file)
        require(name == skill_name, f"frontmatter name mismatch in {skill_file}: {name}")
        require(1 <= len(name) <= 64, f"invalid Agent Skill name length: {name}")
        require(
            re.fullmatch(r"(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name) is not None,
            f"invalid Agent Skill name: {name}",
        )
        require(1 <= len(description) <= 1024, f"invalid description length in {skill_file}")
        frontmatter_names.append(name)
    require(len(frontmatter_names) == len(set(frontmatter_names)), "duplicate skill names")


def validate_portable(source_root: Path, output_root: Path) -> None:
    package = output_root / build.PORTABLE_REL
    require(package.is_dir(), f"missing portable package: {package}")
    require(
        {path.name for path in package.iterdir()} == {"plugin.json", "skills"},
        "portable root must contain only plugin.json and skills/",
    )
    manifest = read_json(package / "plugin.json")
    schema = read_json(source_root / "schemas/agent-plugins/1.0.0/plugin.schema.json")
    require(schema.get("$id") == build.AGENT_PLUGINS_SCHEMA, "pinned schema $id mismatch")
    validate_json_schema(manifest, schema)
    require(manifest.get("$schema") == build.AGENT_PLUGINS_SCHEMA, "portable schema mismatch")
    require(manifest.get("name") == build.PLUGIN_NAME, "portable plugin name mismatch")
    require(manifest.get("version") == build.PLUGIN_VERSION, "portable plugin version mismatch")
    require(manifest.get("author") == build.PLUGIN_AUTHOR, "portable author mismatch")
    require(manifest.get("repository") == build.PLUGIN_REPOSITORY, "portable repository mismatch")
    forbidden_manifest_fields = {"skills", "commands", "agents", "rules", "hooks", "mcpServers", "apps"}
    require(
        not (set(manifest) & forbidden_manifest_fields),
        f"portable manifest has forbidden fields: {set(manifest) & forbidden_manifest_fields}",
    )
    validate_skill_set(package / "skills")
    validate_no_symlinks_or_escapes(package)
    validate_markdown_references(package)
    require(not (package / "mcp.json").exists(), "portable package must not contain mcp.json")
    for sidecar in (".codex-plugin", ".claude", "CLAUDE.md", "AGENTS.md"):
        require(not (package / sidecar).exists(), f"portable package contains sidecar {sidecar}")
    require(not any(package.glob("skills/*/agents/openai.yaml")), "portable contains OpenAI metadata")


def validate_openai(source_root: Path, output_root: Path) -> None:
    package = output_root / build.OPENAI_REL
    require(package.is_dir(), f"missing OpenAI-native package: {package}")
    require(
        {path.name for path in package.iterdir()} == {".codex-plugin", "skills"},
        "OpenAI package has unexpected root entries",
    )
    manifest = read_json(package / ".codex-plugin/plugin.json")
    require(
        manifest == {
            "name": build.PLUGIN_NAME,
            "version": build.PLUGIN_VERSION,
            "description": build.PLUGIN_DESCRIPTION,
            "skills": "./skills/",
        },
        f"unexpected OpenAI manifest: {manifest}",
    )
    skills_path = (package / manifest["skills"]).resolve()
    require(build.is_within(skills_path, package), "OpenAI skills path escapes package")
    validate_skill_set(skills_path)
    validate_no_symlinks_or_escapes(package)
    validate_markdown_references(package)
    metadata = read_json(source_root / "src/evenfire/adapters/openai/skills.json")
    for skill_name in build.EXPECTED_SKILLS:
        metadata_path = skills_path / skill_name / "agents/openai.yaml"
        require(metadata_path.is_file(), f"missing OpenAI metadata: {metadata_path}")
        require(
            metadata_path.read_text(encoding="utf-8") == build.openai_yaml(metadata[skill_name]),
            f"OpenAI metadata drift: {skill_name}",
        )


def validate_claude(source_root: Path, output_root: Path) -> None:
    package = output_root / build.CLAUDE_REL
    validate_no_symlinks_or_escapes(package)
    validate_markdown_references(package)
    require(
        {path.name for path in package.iterdir()} == {"CLAUDE.md", ".claude"},
        "Claude distribution has unexpected root entries",
    )
    validate_skill_set(package / ".claude/skills")
    claude_source = source_root / "src/evenfire/adapters/claude-code/CLAUDE.md"
    claude_actual = strip_generated_marker((package / "CLAUDE.md").read_text(encoding="utf-8"))
    require(
        claude_actual == build.add_generated_marker(claude_source.read_text(encoding="utf-8")).replace(
            build.GENERATED_MARKER + "\n\n", ""
        ),
        "generated CLAUDE.md drifted",
    )


def validate_marketplace(source_root: Path, output_root: Path) -> None:
    marketplace = read_json(source_root / build.MARKETPLACE_REL)
    require(set(marketplace) == {"name", "plugins"}, "marketplace root has unexpected fields")
    require(marketplace["name"] == build.PLUGIN_NAME, "marketplace name mismatch")
    require(isinstance(marketplace["plugins"], list) and len(marketplace["plugins"]) == 1,
            "marketplace must contain one plugin")
    entry = marketplace["plugins"][0]
    require(
        entry == {
            "name": build.PLUGIN_NAME,
            "source": {"source": "local", "path": "./dist/openai/evenfire-skills"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity",
        },
        "marketplace plugin entry mismatch",
    )
    require((output_root / build.OPENAI_REL).is_dir(), "staged marketplace target is missing")


def validate_parity(source_root: Path, output_root: Path) -> None:
    source_skills = source_root / "src/evenfire/skills"
    packages = {
        "portable": output_root / build.PORTABLE_REL / "skills",
        "openai": output_root / build.OPENAI_REL / "skills",
        "claude": output_root / build.CLAUDE_REL / ".claude/skills",
    }
    for skill_name in build.EXPECTED_SKILLS:
        source_dir = source_skills / skill_name
        for source_file in build.iter_tree(source_dir):
            if not source_file.is_file():
                continue
            relative = source_file.relative_to(source_dir)
            source_text = source_file.read_text(encoding="utf-8")
            for package_name, package_root in packages.items():
                generated = package_root / skill_name / relative
                # OpenAI adds a generated agents/openai.yaml; skip that synthetic file.
                if relative.as_posix() == "agents/openai.yaml":
                    continue
                require(generated.is_file(), f"{package_name} missing {skill_name}/{relative}")
                if source_file.suffix == ".md":
                    require(
                        strip_generated_marker(generated.read_text(encoding="utf-8")) == source_text,
                        f"{package_name} canonical parity drift: {skill_name}/{relative}",
                    )
                else:
                    require(
                        generated.read_bytes() == source_file.read_bytes(),
                        f"{package_name} canonical parity drift: {skill_name}/{relative}",
                    )


def validate_canonical_source(root: Path) -> None:
    build.validate_source(root)
    source_root = root / "src/evenfire"
    skills_root = source_root / "skills"
    require(
        {path.name for path in (source_root / "adapters/openai").iterdir()} == {"skills.json"},
        "OpenAI adapter contains unused files",
    )
    require(
        {path.name for path in (source_root / "adapters/claude-code").iterdir()} == {"CLAUDE.md"},
        "Claude adapter contains unused files",
    )
    validate_skill_reference_containment(skills_root)
    scan_forbidden_content(skills_root)
    scan_forbidden_content(source_root / "adapters")


def validate_repository_layout(root: Path) -> None:
    require(
        (root / ".gitignore").read_text(encoding="utf-8").splitlines().count("/dist/") == 1,
        "/dist/ must be ignored exactly once",
    )
    tracked_dist = subprocess.run(
        ["git", "ls-files", "dist"], cwd=root, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    require(tracked_dist.returncode == 0, f"git ls-files dist failed: {tracked_dist.stdout}")
    require(not tracked_dist.stdout.strip(), "dist contains tracked files")


def validate_builder_safety(root: Path) -> None:
    scratch = build.scratch_parent(root, None)
    with tempfile.TemporaryDirectory(prefix="validate-builder-safety-", dir=scratch) as temporary:
        temp = Path(temporary)
        fake_repo = temp / "repository"
        outside = temp / "outside"
        source = temp / "source"
        fake_repo.mkdir()
        outside.mkdir()
        source.mkdir()
        (fake_repo / "dist").symlink_to(outside, target_is_directory=True)
        try:
            build.assert_safe_destination(fake_repo, fake_repo / "dist/package")
        except build.BuildError:
            pass
        else:
            raise ValidationError("builder accepted a symlinked output parent")
        (source / "escape").symlink_to(outside, target_is_directory=True)
        try:
            build.assert_safe_source_tree(source)
        except build.BuildError:
            pass
        else:
            raise ValidationError("builder accepted a source symlink")


def run_skills_ref(output_root: Path) -> str:
    executable = shutil.which("skills-ref")
    if executable is None:
        return "skills-ref unavailable; deterministic structural fallback used"
    failures: list[str] = []
    for skill_name in build.EXPECTED_SKILLS:
        skill = output_root / build.PORTABLE_REL / "skills" / skill_name
        result = subprocess.run(
            [executable, "validate", str(skill)], cwd=output_root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if result.returncode != 0:
            failures.append(f"{skill_name}: {result.stdout.strip()}")
    require(not failures, "skills-ref failures:\n" + "\n".join(failures))
    return "skills-ref validate passed for all skills"


def run_subprocess_check(command: list[str], root: Path, label: str) -> None:
    result = subprocess.run(
        command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    require(result.returncode == 0, f"{label} failed:\n{result.stdout}")


def main() -> int:
    root = build.repository_root()
    try:
        validate_canonical_source(root)
        print("PASS: canonical source, reference containment, and OSS hygiene")
        validate_repository_layout(root)
        print("PASS: tracked repository layout")
        validate_builder_safety(root)
        print("PASS: builder path and mode safety")

        scratch = build.scratch_parent(root, None)
        with tempfile.TemporaryDirectory(prefix="validate-evenfire-plugins-", dir=scratch) as temp:
            temporary = Path(temp)
            first = temporary / "first"
            second = temporary / "second"
            first.mkdir()
            second.mkdir()
            build.build_staged_tree(root, first)
            build.build_staged_tree(root, second)

            validate_portable(root, first)
            print("PASS: temporary portable Agent Plugins 1.0.0 package")
            validate_openai(root, first)
            print("PASS: temporary OpenAI-native plugin")
            validate_claude(root, first)
            print("PASS: temporary Claude Code distribution")
            validate_marketplace(root, first)
            print("PASS: repository marketplace resolves temporary OpenAI output")
            validate_parity(root, first)
            print("PASS: canonical portable, OpenAI, and Claude parity")
            scan_forbidden_content(first / "dist")
            print("PASS: generated output OSS hygiene")
            require(
                build.tree_snapshot(first / "dist") == build.tree_snapshot(second / "dist"),
                "independent temporary generations differ",
            )
            print("PASS: deterministic independent regeneration")
            if (root / "dist").exists():
                require(
                    build.tree_snapshot(first / "dist") == build.tree_snapshot(root / "dist"),
                    "local ignored dist is stale",
                )
                print("PASS: local ignored dist is current")
            else:
                print("PASS: clean-clone validation path without local dist")
            print(f"PASS: Agent Skills validation ({run_skills_ref(first)})")

        run_subprocess_check(
            [sys.executable, "scripts/build_evenfire_plugins.py", "--check"], root,
            "deterministic build check",
        )
        print("PASS: build --check")
    except (ValidationError, build.BuildError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("All Evenfire plugin validations passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
