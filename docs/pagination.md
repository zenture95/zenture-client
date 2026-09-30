# Pagination

Public list-style read routes use cursor pagination. The SDK exposes both page
methods and iterator helpers.

## Page Methods

These methods accept the same pagination arguments:

- `client.chat.list(limit=50, cursor=None)`
- `client.chat.messages(chat_id, limit=50, cursor=None)`
- `client.evaluations.list(limit=50, cursor=None)`

`limit` is the requested page size. The default is `50`, the minimum is `1`, and
the maximum is `100`. `cursor` is an opaque string returned by the previous
response. Do not parse, edit, or persist assumptions about cursor structure.

Responses include `next_cursor`. A `None` value means there is no further page.
Paginated public collections are ordered by `created_at desc`.

## Iterator Helpers

Iterator helpers call the page methods repeatedly until `next_cursor` is `None`:

```python
from zenture import ZentureClient

with ZentureClient.from_env() as client:
    for chat in client.chat.iter(limit=50):
        print(chat.chat_id)

    for turn in client.chat.iter_messages("chat_example", limit=50):
        print(turn.turn_id)

    for evaluation in client.evaluations.iter(limit=50):
        print(evaluation.evaluation_id)
```

Async clients expose async iterators with the same names:

```python
from zenture import AsyncZentureClient

async with AsyncZentureClient.from_env() as client:
    async for chat in client.chat.iter(limit=50):
        print(chat.chat_id)
```

The SDK validates `limit` and `cursor` before sending the request, matching the
public OpenAPI bounds. Invalid pagination input raises `ValueError`.
