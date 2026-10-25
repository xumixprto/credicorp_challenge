"""Readable order details for the approval card."""

import json
from html import escape

import streamlit as st

from shared.conversations import OperationConfirmation

LABELS = {
    "qty": "Quantity",
    "notional": "Order amount",
    "type": "Order type",
    "order_type": "Order type",
    "time_in_force": "Time in force",
    "limit_price": "Limit price",
    "stop_price": "Stop price",
    "trail_price": "Trailing amount",
    "trail_percent": "Trailing percent",
    "extended_hours": "Extended hours",
    "order_class": "Order class",
    "take_profit": "Take profit",
    "stop_loss": "Stop loss",
}
TIME_IN_FORCE = {
    "day": "Day",
    "gtc": "Good until canceled",
    "ioc": "Immediate or cancel",
    "fok": "Fill or kill",
    "opg": "Market open",
    "cls": "Market close",
}


def _display_value(name: str, value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return "; ".join(
            f"{LABELS.get(key, key.replace('_', ' ').capitalize())}: {_display_value(key, item)}"
            for key, item in value.items()
        )
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    if name == "time_in_force":
        return TIME_IN_FORCE.get(text.lower(), text.upper())
    if name in {"type", "order_type", "order_class", "side"}:
        return text.replace("_", " ").capitalize()
    return text


def render_operation_summary(confirmation: OperationConfirmation) -> None:
    arguments = confirmation.arguments
    action = confirmation.tool_name.removeprefix("alpaca_")
    side = str(arguments.get("side", "")).lower()
    is_order = action.startswith("place_") and side in {"buy", "sell"}
    title = (
        f"{side.capitalize()} {arguments.get('symbol', 'order')}"
        if is_order
        else action.replace("_", " ").capitalize()
    )
    mode = "paper" if confirmation.paper_trading else "live"
    mode_label = "Paper trading" if confirmation.paper_trading else "Live trading"
    icon = "↗" if side == "buy" and is_order else "↘" if is_order else "⇄"
    detail_cards = []
    for name, value in arguments.items():
        if value is None or name == "client_order_id" or (is_order and name in {"side", "symbol"}):
            continue
        label = LABELS.get(name, name.replace("_", " ").capitalize())
        display = _display_value(name, value)
        if name == "qty" and action == "place_stock_order":
            display += " share" if str(value) == "1" else " shares"
        detail_cards.append(
            f'<div class="approval-detail"><dt>{escape(label)}</dt><dd>{escape(display)}</dd></div>'
        )
    note = "Review the details below before confirming."
    if is_order and arguments.get("type", arguments.get("order_type")) == "market":
        note = "Market order · The execution price is determined when the order fills."
    st.html(
        '<section class="approval-summary" aria-label="Operation review">'
        '<div class="approval-topline"><span class="approval-eyebrow">REVIEW &amp; CONFIRM</span>'
        f'<span class="approval-mode approval-mode--{mode}">{mode_label}</span></div>'
        '<div class="approval-heading">'
        f'<span class="approval-icon" aria-hidden="true">{icon}</span>'
        f"<h3>{escape(title)}</h3></div>"
        f'<p class="approval-description">{escape(note)}</p>'
        f'<dl class="approval-details">{"".join(detail_cards)}</dl>'
        "</section>"
    )
