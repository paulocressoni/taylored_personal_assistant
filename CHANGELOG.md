# Changelog

## [0.3.1](https://github.com/paulocressoni/taylored_personal_assistant/compare/v0.3.0...v0.3.1) (2026-09-26)


### Bug Fixes

* **docker-publish:** grant the SBOM step the permissions to attach release assets ([#27](https://github.com/paulocressoni/taylored_personal_assistant/issues/27)) ([df937f5](https://github.com/paulocressoni/taylored_personal_assistant/commit/df937f5764c2049eb3040c663ffa8761a94ff8b0))

## [0.3.0](https://github.com/paulocressoni/taylored_personal_assistant/compare/v0.2.0...v0.3.0) (2026-09-26)


### Features

* **m28:** langfuse production observability + encrypted backups ([#25](https://github.com/paulocressoni/taylored_personal_assistant/issues/25)) ([08a2706](https://github.com/paulocressoni/taylored_personal_assistant/commit/08a270681d216ffc9e5b83437f2b935395fdf7c8))


### Documentation

* add the profile to the dev down examples and to the backup runbook's stop/start, where a profile-less stop leaves Langfuse's Postgres, ClickHouse, MinIO and Redis writing during a restore - the torn restore that step exists to prevent. The 'network still in use' section now names the missing profile as the usual cause instead of filing it as a Compose quirk. ([08a2706](https://github.com/paulocressoni/taylored_personal_assistant/commit/08a270681d216ffc9e5b83437f2b935395fdf7c8))
