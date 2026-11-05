# Acme Service

Small dependency-free Python service used for coding-agent evaluation.

- Runtime timeout: 60 seconds (legacy documentation; source code is authoritative).
- Retries: 3.
- Usernames are compared case-insensitively.

Run the visible smoke tests with `python -m unittest discover -s tests`.

