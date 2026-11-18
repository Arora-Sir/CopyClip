# Contributing

Thanks for looking. A few rules keep the project reliable, because it sits on the path of everything you copy.

## Ground rules

1. **Never regress the text-sync path.** It is used daily. A new feature is a parallel code path, not a rewrite of the proven one. The byte-level wire format is pinned by `tests/unit/test_android_wire.py` and `tests/unit/test_framing.py`.
2. **Zero third-party dependencies**, in Python and Java. If a task seems to need a library, find the platform-native way first. `tests/unit/test_architecture.py` fails the build on an outside import.
3. **Never run anything as root on a device.**
4. **Measure before you conclude**, and say when something is inferred. Spike unproven platform behaviour in `android/spike/` and write down what was actually measured, including what was not.
5. **Docs are part of the change.** Update `ARCHITECTURE.md` or `README.md`, and add a write-up in `docs/bugs/` for anything that needed a spike or fixed a real defect. A bug stays open until it is confirmed in real use.
6. **No personal data.** Use placeholder addresses (`100.64.0.x`) and `C:\Users\you\...` in code and docs. `python tools/privacy_scan.py` must report nothing.

## Workflow

```bash
python tools/run_tests.py          # must pass before and after your change
python tools/privacy_scan.py       # must report 0 findings
python tools/demo_ui.py --open     # look at the window without any device
```

- Write the failing test first. Prefer the shared doubles in `tests/fakes.py` over new ones.
- Tests must not sleep to wait for behaviour. Poll with a deadline, or use `FakeClock`.
- Layering is enforced by `tests/unit/test_architecture.py`: `core/` never imports `nodes/`, `domains/`, `transports/` or `windows_app/`.
- Keep commits small and describe the why. Run the system and device tiers by hand for anything touching the clipboard or a device (see `tests/README.md`).

## Style

Plain, direct comments that explain why, not what. No em dashes, straight quotes only.
