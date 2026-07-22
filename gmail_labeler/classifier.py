"""Email classification and job-detail extraction.

Classification is one call per email using structured outputs; the JSON schema
is built from prompts/categories.md, so the model can only return a valid
category/subcategory. "other" means "leave it in the inbox".

Application-confirmation emails get a second, targeted extraction call whose
instructions live in prompts/extraction.md.

The actual model call is delegated to an llm client (see gmail_labeler/llm.py),
so this module is provider-agnostic: it only builds prompts and schemas.
"""

import logging

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 1500
NO_LABEL_CATEGORY = "other"

_NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "company": _NULLABLE_STR,
        "role_title": _NULLABLE_STR,
        "req_number": _NULLABLE_STR,
        "role_type": {
            "anyOf": [
                {"type": "string", "enum": ["internship", "co_op", "full_time", "part_time"]},
                {"type": "null"},
            ]
        },
        "season": _NULLABLE_STR,
    },
    "required": ["company", "role_title", "req_number", "role_type", "season"],
    "additionalProperties": False,
}


def email_content(email: dict) -> str:
    body = (email.get("body") or "")[:MAX_BODY_CHARS]
    return (
        f"From: {email.get('sender', '')}\n"
        f"Subject: {email.get('subject', '')}\n\n"
        f"{body}"
    )


def build_system_prompt(categories: dict) -> str:
    lines = [
        "You are an email classifier for a personal Gmail inbox.",
        "Classify the email into exactly one category. When the category defines",
        "subcategories, also pick the single subcategory that clearly applies;",
        "if none clearly applies, use an 'unsure'-style subcategory if the",
        "category has one, otherwise null.",
        "",
        "Categories:",
    ]
    for slug, spec in categories.items():
        lines.append(f"- {slug}: {spec['description'].replace(chr(10), ' ')}")
        for sub_slug, sub_spec in spec["subcategories"].items():
            lines.append(f"    - {sub_slug}: {sub_spec['description'].replace(chr(10), ' ')}")
    lines += [
        f"- {NO_LABEL_CATEGORY}: Anything that does not clearly fit a category above.",
        "",
        "Confidence refers to the CATEGORY choice only: set it to 'low' when the",
        "email might not belong to the chosen category at all. An uncertain",
        f"subcategory alone is not low confidence. When genuinely unsure of the",
        f"category, prefer '{NO_LABEL_CATEGORY}'.",
    ]
    return "\n".join(lines)


def build_output_schema(categories: dict) -> dict:
    cat_enum = list(categories) + [NO_LABEL_CATEGORY]
    sub_enum = sorted({s for spec in categories.values() for s in spec["subcategories"]})
    if sub_enum:
        sub_schema = {"anyOf": [{"type": "string", "enum": sub_enum}, {"type": "null"}]}
    else:
        sub_schema = {"type": "null"}
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": cat_enum},
            "subcategory": sub_schema,
            "confidence": {"type": "string", "enum": ["high", "low"]},
        },
        "required": ["category", "subcategory", "confidence"],
        "additionalProperties": False,
    }


def classify(client, system_prompt: str, schema: dict, email: dict) -> dict | None:
    return client.structured(system_prompt, schema, email_content(email))


def extract_job_details(client, extraction_prompt: str, email: dict) -> dict | None:
    return client.structured(extraction_prompt, EXTRACTION_SCHEMA, email_content(email))


def validate_result(result: dict, categories: dict) -> dict:
    """Null out a subcategory that doesn't belong under the chosen category."""
    cat = result.get("category")
    sub = result.get("subcategory")
    if sub and sub not in categories.get(cat, {}).get("subcategories", {}):
        result["subcategory"] = None
    return result
