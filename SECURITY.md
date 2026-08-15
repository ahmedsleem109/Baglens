# Security policy

## Reporting a vulnerability

Report privately through GitHub's [Security Advisories][advisories] on this repository, or
by email to the address on the maintainer's GitHub profile. Please do not open a public
issue for a vulnerability.

Include what you did, what happened, and the recording or input that triggered it if you
can share one. A synthetic reproduction from `tests/synth/generate.py` is ideal, because
robot recordings are rarely shareable.

Expect an acknowledgement within a week. If a fix is warranted it will ship in a patch
release with credit, unless you would rather not be named.

[advisories]: https://github.com/ahmedsleem109/Baglens/security/advisories/new

## What this project's threat model actually is

baglens reads recordings and answers questions about them. It is **read-only by
construction** — there is no writer code path in `src/baglens` — and it is designed to be
pointed at files a robot produced, which is to say files whose contents nobody controlled
and nobody validated.

The interesting attack surface is therefore *parsing*, and the assumption you should make
is the honest one: **a malicious recording is untrusted input to third-party parsers.**
MCAP, SQLite, ROS 1 bag and ULog decoding happen inside `mcap`, `rosbags`, `duckdb` and
`pyulog`. A crafted file that crashes or exploits one of those is a vulnerability in that
library first, and something this project should contain second. Reports of either are
welcome.

Concretely, in scope:

* A recording that causes unbounded memory or CPU use — the detector library promises
  fixed-size state per topic, and a file that defeats that bound is a real bug.
* A path in a recording, a topic name, or a message field that escapes `--root`
  confinement (`Config.resolve`) or reaches the filesystem outside it.
* Redaction failures: a topic or field named in `BAGLENS_REDACT_TOPICS` /
  `BAGLENS_REDACT_FIELDS` that still appears in a tool response.
* Anything that turns a read into a write.

Out of scope, and deliberately so:

* Denial of service from a legitimately enormous recording. Auditing a 40 GB bag takes as
  long as it takes.
* The MCP server binding a port when you pass `--http`. It binds `127.0.0.1` by default;
  exposing it to a network is your decision and there is no authentication layer.
* Findings you disagree with. A wrong verdict is a correctness bug — please file it as a
  normal issue, with the recording if you can, since those are the reports that have
  improved the detectors most.

## Running it on untrusted recordings

If you audit files you did not produce, use the confinement that exists:

```bash
baglens --root /srv/recordings --no-frames
```

`--root` refuses every read outside the named directories. `--no-frames` disables image
extraction entirely, which removes the image decoders from the attack surface and stops
camera frames leaving the process. Both are also environment variables
(`BAGLENS_ROOTS`, `BAGLENS_NO_FRAMES=1`) for containerised deployments.

## Supported versions

Until 1.0, only the latest release receives fixes.
