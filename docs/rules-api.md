# Rules manager API

Real responses from `scripts/rules_api_doc.py` on the synthetic DB in `tests/rules_scene.py` (5 rules:
3 AI, 2 yours; 2 suggestions). All POSTs need `x-csrf-token` with the built-in login; errors are
`{"error": str}` with 400 (bad input), 404 (unknown rule / suggestion / Undo receipt) or 409 (that Undo
was used already, or a rule with its pattern exists again).

Routes: `GET /api/rules`; `POST /api/rules/update {id, category, pattern?}` (pattern only for user rules, else 400);
`POST /api/rules/keep {id}`; `POST /api/rules/delete {id}` -> `{deleted, rule, filed, undo}`;
`POST /api/rules/restore {undo}` -> `{rule, relinked}`; `POST /api/rules/suggestions/accept {key}` -> `{rule, refiled}`;
`POST /api/rules/suggestions/dismiss {key}`. Each write is one transaction: it all happens or none of it does.

Undo: `undo` is `{"receipt": str}`, the handle of the server's own record of the delete (table rule_deletions; only
the receipt's SHA-256 is stored). Restore reads nothing else: any other field, or any other shape, is a 400; an unknown
receipt is a 404; a receipt restores once, and a second try (or a rule with the same pattern saved since) is a 409. The
rule comes back as it was, and only the rows it filed that no one changed since are linked to it again (`relinked`).
The receipt survives a restart. The rows a deleted rule filed keep their category; a deleted user rule's rows stay
the user's (the AI never changes them), a deleted AI rule's rows stay open to the AI.

Accepting a suggestion saves that merchant's rule and re-files only the rows it matches (`refiled` counts those).
Editing an AI rule's category makes it yours (source becomes "user"). `matches` counts live transactions the rule
matches now (user-filed ones included).

## 1. GET /api/rules

HTTP 200

```json
{
  "rules": [
    {
      "id": 5,
      "field": "merchant_name",
      "type": "exact",
      "pattern": "Blue Bottle",
      "label": "Merchant is Blue Bottle",
      "category": "Eating Out",
      "source": "user",
      "matches": 3,
      "created_at": "2026-09-05T12:00:00Z"
    },
    {
      "id": 4,
      "field": "merchant_name",
      "type": "exact",
      "pattern": "Netflix",
      "label": "Merchant is Netflix",
      "category": "Entertainment",
      "source": "user",
      "matches": 3,
      "created_at": "2026-09-04T12:00:00Z"
    },
    {
      "id": 3,
      "field": "merchant_name",
      "type": "exact",
      "pattern": "Equinox",
      "label": "Merchant is Equinox",
      "category": "Entertainment",
      "source": "ai",
      "matches": 3,
      "created_at": "2026-09-03T12:00:00Z"
    },
    {
      "id": 2,
      "field": "name",
      "type": "regex",
      "pattern": "OPENROUTER",
      "label": "Name contains OPENROUTER",
      "category": "Subscriptions",
      "source": "ai",
      "matches": 2,
      "created_at": "2026-09-02T12:00:00Z"
    },
    {
      "id": 1,
      "field": "merchant_name",
      "type": "exact",
      "pattern": "Xai",
      "label": "Merchant is Xai",
      "category": "Subscriptions",
      "source": "ai",
      "matches": 3,
      "created_at": "2026-09-01T12:00:00Z"
    }
  ],
  "counts": {
    "total": 5,
    "user": 2,
    "ai": 3
  },
  "suggestions": [
    {
      "key": "merchant_name|equinox|8",
      "merchant": "Equinox",
      "category": "Other Health",
      "times": 2,
      "text": "You moved Equinox from the AI's pick to Other Health 2 times. Always do that?"
    },
    {
      "key": "merchant_name|trader joe's|2",
      "merchant": "Trader Joe's",
      "category": "Groceries",
      "times": 2,
      "text": "You filed Trader Joe's under Groceries 2 times. Always do that?"
    }
  ]
}
```

## 2. POST /api/rules/update `{"id": 3, "category": "Other Health"}`

HTTP 200

```json
{
  "rule": {
    "id": 3,
    "field": "merchant_name",
    "type": "exact",
    "pattern": "Equinox",
    "label": "Merchant is Equinox",
    "category": "Other Health",
    "source": "user",
    "matches": 3,
    "created_at": "2026-09-03T12:00:00Z"
  },
  "refiled": 1
}
```

## 3. POST /api/rules/delete `{"id": 3}`

HTTP 200

```json
{
  "deleted": 3,
  "rule": {
    "id": 3,
    "field": "merchant_name",
    "type": "exact",
    "pattern": "Equinox",
    "label": "Merchant is Equinox",
    "category": "Other Health",
    "source": "user"
  },
  "filed": [
    "t16"
  ],
  "undo": {
    "receipt": "<receipt: 32 random characters, returned once>"
  }
}
```

## 4. POST /api/rules/restore `{"undo": {"receipt": "<receipt: 32 random characters, returned once>"}}`

HTTP 200

```json
{
  "rule": {
    "id": 3,
    "field": "merchant_name",
    "type": "exact",
    "pattern": "Equinox",
    "label": "Merchant is Equinox",
    "category": "Other Health",
    "source": "user",
    "matches": 3,
    "created_at": "2026-09-03T12:00:00Z"
  },
  "relinked": 1
}
```

## Error example: pattern edit on an AI rule

HTTP 400

```json
{
  "error": "only your own rules' pattern can be changed (keep this rule first)"
}
```

## Error example: the same Undo again

HTTP 409

```json
{
  "error": "undo: that delete was already undone"
}
```
