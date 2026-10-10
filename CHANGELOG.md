# Changelog

## [0.4.0](https://github.com/paulocressoni/taylored_personal_assistant/compare/v0.3.1...v0.4.0) (2026-10-10)


### Features

* **observability:** give STT and TTS their own spans in the voice turn waterfall ([#38](https://github.com/paulocressoni/taylored_personal_assistant/issues/38)) ([929b049](https://github.com/paulocressoni/taylored_personal_assistant/commit/929b04969726956c113484d712b93a07bd9eb3fa))
* **voice:** add the spoken voice pipeline (M29) ([#29](https://github.com/paulocressoni/taylored_personal_assistant/issues/29)) ([a48bcbc](https://github.com/paulocressoni/taylored_personal_assistant/commit/a48bcbc2553c3e3c29480ae555d54c6433359051))


### Bug Fixes

* **observability:** show the turn's question, answer and outcome on every trace ([#34](https://github.com/paulocressoni/taylored_personal_assistant/issues/34)) ([0659efd](https://github.com/paulocressoni/taylored_personal_assistant/commit/0659efdfa76812fe3bf5fc34613e96bc21fad92d))
* **voice:** close the turn a barge-in interrupts ([#36](https://github.com/paulocressoni/taylored_personal_assistant/issues/36)) ([1169f31](https://github.com/paulocressoni/taylored_personal_assistant/commit/1169f31f47693f533d54b4177a9e6c84d16d5c23))


### Performance Improvements

* **voice:** speak the first chunk at a clause break ([#37](https://github.com/paulocressoni/taylored_personal_assistant/issues/37)) ([e819e0a](https://github.com/paulocressoni/taylored_personal_assistant/commit/e819e0a5fc84ec8e833237449e4913cb3fb1826c))

## [0.3.1](https://github.com/paulocressoni/taylored_personal_assistant/compare/v0.3.0...v0.3.1) (2026-09-26)


### Bug Fixes

* **docker-publish:** grant the SBOM step the permissions to attach release assets ([#27](https://github.com/paulocressoni/taylored_personal_assistant/issues/27)) ([df937f5](https://github.com/paulocressoni/taylored_personal_assistant/commit/df937f5764c2049eb3040c663ffa8761a94ff8b0))

## [0.3.0](https://github.com/paulocressoni/taylored_personal_assistant/compare/v0.2.0...v0.3.0) (2026-09-26)


### Features

* **m28:** langfuse production observability + encrypted backups ([#25](https://github.com/paulocressoni/taylored_personal_assistant/issues/25)) ([08a2706](https://github.com/paulocressoni/taylored_personal_assistant/commit/08a270681d216ffc9e5b83437f2b935395fdf7c8))


### Documentation

* add the profile to the dev down examples and to the backup runbook's stop/start, where a profile-less stop leaves Langfuse's Postgres, ClickHouse, MinIO and Redis writing during a restore - the torn restore that step exists to prevent. The 'network still in use' section now names the missing profile as the usual cause instead of filing it as a Compose quirk. ([08a2706](https://github.com/paulocressoni/taylored_personal_assistant/commit/08a270681d216ffc9e5b83437f2b935395fdf7c8))
