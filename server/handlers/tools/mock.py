"""Mock engine — explicit opt-in only executor wrapper.

Restored minimal version (2026-08-18): passthrough; recording of canned
responses was lost with the storm-era PR. Enabling mock changes nothing
until canned-response storage is implemented.
"""


class MockEngine:
    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def with_overrides(self, overrides):
        return MockEngine({**self.cfg, **(overrides or {})})

    def wrap(self, executor):
        return executor  # passthrough until canned-response store exists
