# wheels/

`aiortc-1.14.0+av17-py3-none-any.whl` is a byte-identical repackage of the
official pure-Python [aiortc](https://github.com/aiortc/aiortc) 1.14.0 wheel
with a single metadata line relaxed: the `av<17` cap becomes `av<19`.

Home Assistant pins `av==17.x`, which makes every upstream aiortc release
unresolvable in HA's pip environment even though aiortc 1.14 works fine with
av 17 at runtime. The integration's `manifest.json` installs this wheel
directly by URL with a `#sha256=` integrity pin.

The wheel is committed to the repo (rather than attached to a GitHub release)
because HACS treats any release — including pre-releases — as an integration
version and would offer the wheel-hosting tag as a bogus "update".

**Remove this directory and restore a plain `aiortc>=...` requirement once an
aiortc release allows `av>=17`** — watch upstream's `pyproject.toml`.
