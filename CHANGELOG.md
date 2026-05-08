# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once the
API stabilises.

## [Unreleased]

## [0.1.0] — 2026-05-07

Initial open-source release.

### Added
- MIT license.
- English-language README, CONTRIBUTING, CODE_OF_CONDUCT.
- `.env.example` as a placeholder-only template.
- Public project layout documentation under `docs/`.

### Changed
- Externalised all AWS account IDs, S3 buckets, CloudFront IDs, Cognito
  identifiers, and personal email addresses from configs and scripts into
  environment variables.
- Tightened `.gitignore` to prevent future secrets from being committed.

### Security
- Rotated every credential previously present in the codebase. Git history
  has been rewritten to expunge leaked values.
