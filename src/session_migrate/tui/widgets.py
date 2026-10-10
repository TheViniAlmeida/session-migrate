"""Literal terminal text widgets: never interpret conversation markup."""

from rich.text import Text
from textual.widgets import Static

from session_migrate.preview import literal_text


def plain(value: object) -> Text:
    return Text(literal_text(str(value)))


class Literal(Static):
    def __init__(self, value: str = "", **kwargs):
        super().__init__(plain(value), markup=False, **kwargs)

    def set_text(self, value: str) -> None:
        self.update(plain(value))
