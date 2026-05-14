# Changelog

All notable changes to `pfc-mcp-bridge` are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] - 2026-05-14

Compatibility release shipping alongside an updated `addon.py` bootstrap that
falls back to a Tsinghua mirror when PyPI is unreachable, so PFC 6/7 users
behind corporate proxies or slow international routes can install the bridge
reliably. No code changes to the bridge package itself.
