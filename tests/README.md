# CopyClip tests

Run everything that is safe with one command:

```
python tools/run_tests.py
```

## Tiers

| Tier | Where | Touches | How to run |
|------|-------|---------|------------|
| unit | `tests/unit/` and `tests/test_android_node.py` | Nothing outside the process (loopback sockets only) | `python tools/run_tests.py` |
| system | `test_core.py`, `test_imaging.py`, `test_sensitive_sync.py` | The real Windows clipboard, WIC, the single-instance mutex | `COPYCLIP_SYSTEM_TESTS=1 python tools/run_tests.py --system` with the tray app closed |
| device | `test_e2e.py`, `test_three_device.py`, `test_autonomous_mesh.py` and the other hardware scripts | Real phones and tablets | `COPYCLIP_DEVICE_TESTS=1 python tools/run_tests.py --device tests/test_e2e.py` |

The system tier overwrites your clipboard, and a running CopyClip would sync those clips to your devices. The runner refuses to start it while the daemon is up.

## Shared doubles

`tests/fakes.py` holds `FakeNode`, `FakeTransport` and `FakeClock`. Prefer these over new per-file fakes.

## Known-defect tests

A test decorated with `@unittest.expectedFailure` pins a defect that is documented in the plan and not fixed yet. When the fix lands, the test starts passing unexpectedly, the runner reports it, and the decorator gets removed in the same change.

## Rules for new tests

- A unit test must never touch the clipboard, a device or a fixed port. Use `FakeNode`, `FakeTransport` and the loopback helpers in `test_network.py`.
- No `time.sleep` to wait for behaviour. Poll with a deadline, or patch the clock with `FakeClock`.
- Characterisation tests (wire formats, current quirks) say so in a comment, so a deliberate behaviour change is visible in review.
