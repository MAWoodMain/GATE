# Roadmap

## Next hardware milestone

1. USB permissions, identity, and all six scalar queries now work on the attached SDS1202X-E. Next compare readings against its front panel and known signals, and expand fixtures for missing signal and disabled channels.
2. Native SDS1202X-E waveform snapshots now have bounded binary framing, calibrated relative axes and protocol fixtures. Channel-aware polling, single-shot trigger readiness/freshness checks and scope screenshots are also protocol-tested. Next verify these paths on hardware and calibrated signals.
3. Validate the GNOME top bar on Ubuntu 24.04 GNOME 46 and newer target Shell releases. Add persistent channel selection and compact engineering-unit formatting.
4. Fluke 45 primary serial acquisition is now verified; next validate dual-display/modifier variants and add the first bench PSU after its exact model is known. Include channel capabilities, limits, explicit arming, readback checks, and verified stop behavior.

5. Hantek 365 direct USB discovery, temperature acquisition, Celsius/Fahrenheit switching, recording and passive experiments are verified on a 365B with the supplied thermocouple. Next compare against calibrated references and verify electrical ranges and other family models.

## Bench workflows

- Experiments now have compatible equipment selection, parameterised capture/converter procedures, verified-control/limit checks, settled acquisition, derived metrics and persistent reports/charts. The paired simulator exercises converter sweeps. Next add real verified PSU/load drivers, channel-level assignments, additional procedure types and configurable plot templates.

- Persistent connections and named sessions; live plots, event timeline, channel selection and units.
- Sweep plans with current limits, settle conditions, failure policies, and UI acknowledgement of actual hardware state.
- Export event streams and device/firmware metadata; indexing and retention for long recordings.
- USB hotplug/reconnect with backoff and explicit output recovery policy.
- VXI-11/HiSLIP via an optional VISA transport, additional serial protocols, GPIB, and service discovery.

## Scale and distribution

- Concurrent acquisition workers with one owner per instrument/transport and measured timing uncertainty.
- Capability-based UI, model catalog, driver validation fixtures, and a published plugin compatibility policy.
- Hardware trigger/clock synchronization where supported, distinct from host-timestamp alignment.
- Ubuntu package and release artifacts, integration tests on supported GNOME versions, accessible/localized UI, and long-duration tests.
