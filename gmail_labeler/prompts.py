"""Load the user-editable prompt files from the prompts/ directory.

categories.md is the taxonomy's source of truth: `## Name` starts a category,
`### Name` a subcategory, an `archive: yes|no` line under a category heading
controls inbox removal, and the remaining text is the model-facing description.
"""

import re
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def prompts_dir(config: dict) -> Path:
    override = config.get("prompts_dir")
    return Path(override).expanduser() if override else DEFAULT_PROMPTS_DIR


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", title.strip().lower()).strip("_")


def load_categories(path: Path) -> dict:
    """Parse categories.md into {slug: {title, description, archive, subcategories}}.

    Text before the first `## ` heading (the format documentation) is ignored.
    """
    categories: dict = {}
    cat = sub = None
    for raw in path.read_text().splitlines():
        line = raw.rstrip()
        if line.startswith("## ") and not line.startswith("### "):
            title = line[3:].strip()
            cat = {"title": title, "description": [], "archive": False, "subcategories": {}}
            sub = None
            categories[_slug(title)] = cat
        elif line.startswith("### "):
            if cat is None:
                continue
            title = line[4:].strip()
            sub = {"title": title, "description": []}
            cat["subcategories"][_slug(title)] = sub
        elif line.startswith("#"):
            continue
        elif cat is not None:
            m = re.fullmatch(r"archive:\s*(yes|no|true|false)", line.strip(), re.IGNORECASE)
            if m and sub is None:
                cat["archive"] = m.group(1).lower() in ("yes", "true")
                continue
            (sub if sub is not None else cat)["description"].append(line)

    for c in categories.values():
        c["description"] = "\n".join(c["description"]).strip()
        for s in c["subcategories"].values():
            s["description"] = "\n".join(s["description"]).strip()

    if not categories:
        raise SystemExit(f"No categories found in {path} (expected '## Name' headings)")
    return categories


def load_extraction_prompt(path: Path) -> str:
    return path.read_text().strip()


def category_label(spec: dict) -> str:
    return spec["title"]


def subcategory_label(cat_spec: dict, sub_spec: dict) -> str:
    return f"{cat_spec['title']}/{sub_spec['title']}"


def all_label_names(categories: dict) -> list[str]:
    names = []
    for spec in categories.values():
        names.append(category_label(spec))
        for sub_spec in spec["subcategories"].values():
            names.append(subcategory_label(spec, sub_spec))
    return names
