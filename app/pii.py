"""Replace buyer PII with stable tokens before anything reaches an LLM."""

import re

KEY_KIND = {
    "email": "EMAIL", "email_address": "EMAIL",
    "given_name": "NAME", "surname": "NAME", "full_name": "NAME",
    "phone": "PHONE", "phone_number": "PHONE", "national_number": "PHONE",
    "address_line_1": "ADDRESS", "address_line_2": "ADDRESS", "admin_area_1": "ADDRESS",
    "admin_area_2": "ADDRESS", "postal_code": "ADDRESS",
}
# A plain string "name" is a person only under these parents (items[].name is a product).
PERSON_PARENTS = {"buyer", "payer", "seller", "customer", "recipient"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# ponytail: needs separators (+1 415 555 0100, (415) 555-0100) so tracking numbers and IDs survive.
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}")


class Redactor:
    def __init__(self):
        self._token_for: dict[tuple[str, str], str] = {}
        self._value_for: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        key = (kind, value)
        if key not in self._token_for:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"<{kind}_{self._counts[kind]}>"
            self._token_for[key] = token
            self._value_for[token] = value
        return self._token_for[key]

    def _text(self, s: str) -> str:
        s = EMAIL_RE.sub(lambda m: self._token("EMAIL", m.group()), s)
        return PHONE_RE.sub(lambda m: self._token("PHONE", m.group()), s)

    def redact(self, obj, parent: str | None = None):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                kind = KEY_KIND.get(k) or ("NAME" if k == "name" and parent in PERSON_PARENTS else None)
                out[k] = self._token(kind, v) if kind and isinstance(v, str) and v else self.redact(v, k)
            return out
        if isinstance(obj, list):
            return [self.redact(v, parent) for v in obj]
        if isinstance(obj, str):
            return self._text(obj)
        return obj

    def restore(self, text: str) -> str:
        for token, value in self._value_for.items():
            text = text.replace(token, value)
        return text
