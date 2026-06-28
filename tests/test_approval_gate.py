# SPDX-License-Identifier: Apache-2.0
"""The approval gate must classify risk from GROUND-TRUTH element facts (the element's
own name/type), never an opaque DOM ref or a model-supplied label. These are
dependency-free unit tests of the classifier (`_risk_for` / `_max_risk`); the live
DOM/vision resolution is covered by the real-Chromium E2E in test_sdk.py."""

from __future__ import annotations

import pytest

from agenticbrowser.agent import _max_risk, _risk_for
from agenticbrowser.models import Risk


@pytest.mark.parametrize(
    "name,expected",
    [
        # destructive (financial / irreversible) — matched on the real element name
        ("Place Order", Risk.DESTRUCTIVE),
        ("Place Order — $128.40", Risk.DESTRUCTIVE),
        ("Pay now", Risk.DESTRUCTIVE),
        ("Buy now", Risk.DESTRUCTIVE),
        ("Complete purchase", Risk.DESTRUCTIVE),
        ("Confirm order", Risk.DESTRUCTIVE),
        ("Checkout", Risk.DESTRUCTIVE),
        ("Delete account", Risk.DESTRUCTIVE),
        ("Transfer funds", Risk.DESTRUCTIVE),
        ("Wire $500", Risk.DESTRUCTIVE),
        ("Unsubscribe", Risk.DESTRUCTIVE),
        ("Deactivate account", Risk.DESTRUCTIVE),
        # benign / common false-positive traps that must NOT gate
        ("Read more", Risk.SAFE),
        ("Add to cart", Risk.SAFE),
        ("Search", Risk.SAFE),
        ("Order history", Risk.SAFE),
        ("My orders", Risk.SAFE),
        ("Remove from cart", Risk.SAFE),
        # whole-word matching: substrings must not trip the gate
        ("paypal", Risk.SAFE),
        ("Resend code", Risk.SAFE),
        # escape hatches — never destructive (don't trap the user in a dialog)
        ("Cancel", Risk.SAFE),
        ("Cancel order", Risk.SAFE),
        ("Close", Risk.SAFE),
        ("No thanks", Risk.SAFE),
        # sensitive (no pause, but flagged)
        ("Sign in", Risk.SENSITIVE),
        ("Submit", Risk.SENSITIVE),
    ],
)
def test_risk_for_names(name, expected):
    assert _risk_for(name=name, kind="click") is expected


def test_opaque_ref_is_never_destructive():
    # the OLD bug classified on the opaque DOM ref ("e5"); that path must be SAFE now.
    for ref in ("e5", "e12", "e0"):
        assert _risk_for(name=ref, kind="click") is Risk.SAFE


def test_password_field_is_sensitive():
    assert _risk_for(name="", input_type="password", kind="type") is Risk.SENSITIVE
    assert _risk_for(name="", input_type="email", kind="type") is Risk.SENSITIVE


def test_type_and_select_are_sensitive():
    assert _risk_for(name="Search", kind="type") is Risk.SENSITIVE
    assert _risk_for(name="Country", kind="select") is Risk.SENSITIVE


def test_max_risk_orders_correctly():
    assert _max_risk(Risk.SAFE, Risk.DESTRUCTIVE) is Risk.DESTRUCTIVE
    assert _max_risk(Risk.DESTRUCTIVE, Risk.SAFE) is Risk.DESTRUCTIVE
    assert _max_risk(Risk.SENSITIVE, Risk.SAFE) is Risk.SENSITIVE
    assert _max_risk(Risk.SAFE, Risk.SAFE) is Risk.SAFE


def test_label_only_escalates_never_lowers():
    # how click_at combines ground truth with the model's label:
    # empty ground + destructive label -> escalate to destructive
    assert _max_risk(_risk_for(name=""), _risk_for(name="Pay")) is Risk.DESTRUCTIVE
    # destructive ground + benign/empty label -> stays destructive (label can't lower it)
    assert _max_risk(_risk_for(name="Place Order"), _risk_for(name="")) is Risk.DESTRUCTIVE
    assert _max_risk(_risk_for(name="Delete account"), _risk_for(name="ok")) is Risk.DESTRUCTIVE
