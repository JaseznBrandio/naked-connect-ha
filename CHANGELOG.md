# Changelog

## 0.1.1

- Fixed: the salt reading went back to "Testing…" a while after a test. The unit never reports the end of a test, so Home Assistant now treats the test as finished once its result is in (or after 3 minutes).

## 0.1.0

First release.

- Naked Connect sidebar page with power, mode, output, chlorine boost, salt reading and test, status readings, copper ioniser and pH dosing.
- Live updates from the cloud: changes made in the Naked Connect app or on the unit show within a second, with polling as a fallback.
- Copper ioniser: copper output, copper level, start and stop copper boost with a countdown, working and boost status.
- Salt test: takes the typical reading from the test, so the brief low reading while the cell changes direction is ignored, and shows the unit's verdict ("TDS perfect", "Check and add salt", "TDS high but OK").
- Changes show straight away and are held until the unit confirms them; a rejected change reverts.
- Plain-word status for faults, for example "Low flow".
