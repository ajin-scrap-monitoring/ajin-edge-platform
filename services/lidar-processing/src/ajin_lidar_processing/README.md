# Processing contract

`ProcessingEngine(config).ingest(ScanFrame)` returns acceptance. `measure(now_monotonic_ns,
now_unix_ms, clock, measurement_id=None, cycle_id=None)` returns one envelope or
None for unchanged input, a bounded pairing wait, or revision quarantine. Pure
engine times and optional IDs support deterministic replay. Clock is a mapping
with state and offset_ms. Every sample and calibration uses millimetres.

See repository-root deploy/config.example/processing.example.json for a deliberately
fake four-bin example. It is not installation calibration. Runtime refuses
demo calibration without allow_demo_calibration true. Field configurations must
include measured extrinsics, section geometry and validated mappings.

Per sensor calibration: rotation is a finite proper orthonormal 3x3 matrix;
translation_mm has three components. Site section uses x and z. roi_x_mm span
is a multiple of 50mm; bottom_mm and max_height_mm have one value per 50mm bin.
roi_z_mm bounds height; optional roi_polygon_xz_mm restricts a nonrectangular
section. masks_x_mm exclude whole profile columns; mask_polygons_xz_mm exclude
site-plane objects. Column masks must align with 50mm bin edges and lie inside
roi_x_mm. Fixed mask polygons support only four-corner axis-aligned rectangles
spanning the entire roi_z_mm interval, with x edges on 50mm bin boundaries.
These become excluded columns in eligibility, coverage, area and interpolation.
Partial-height, partial-column or nonrectangular fixed masks are rejected with a
full-column calibration error. Arbitrary finite polygons remain supported for
the ROI itself. No fixed-mask geometry is inferred from field observations.

calibration.version identifies field calibration. fusion_map and optional
single_sensor_maps keyed by sensor_id are ordered [input,output] knots, domain
endpoints 0 and 1, monotone range [0,1]. Single-sensor output is disabled unless
a separate map exists. Positive sensor base_weight values must sum to one.
processing.target_scan_hz defaults to 10.

Each sensor may set sample_filter {distance_min_mm, distance_max_mm,
quality_min, angle_interval_mdeg}. Defaults are 1mm, 100000mm, quality10,
and [0,360000). The angle interval is a non-wrapping [start,end) sector;
configure the installed measurement sector to exclude unrelated 360-degree
returns. Invalid distance or quality within that expected sector counts against
valid_sample_ratio even when no geometric point can be located. Valid points
outside the calibrated site ROI or inside fixed masks are excluded.

Up to ten recent scans are held, and the last five within two seconds contribute
bin medians. A three-MAD filter with 10mm floor rejects transient heights; a
three-of-five sustained movement changes the median. Only interior gaps of up
to 150mm interpolate; original observed coverage still reduces confidence.
Any remaining missing active column invalidates the cross-section. Input pairs
must be within 500ms; partial input waits at most 500ms. Unchanged sensor frames
cannot produce fresh valid output. Monotonic age and UTC-vs-monotonic drift are
checked at measurement time. New instance IDs reset sequence history.
ingest additionally accepts keyword receive_monotonic_ns and receive_unix_ms;
both are required together. Runtime supplies these for immediate age/drift
rejection. Replay can omit them: frames remain candidates until measure validates
the injected clocks, and only valid frames commit sequence/time history. Future
frames cannot prevent valid same-instance recovery. Malformed protocol-wide
angles outside [0,360000) reject the entire frame before sector filtering.
Normal recent-ten history rotation increments history_evictions, not frame_loss.
The missing-peer wait starts once and does not reset with each healthy scan,
so a continuously active single sensor can use its calibrated fallback.

Runtime CLI: `lidar-processing --config PATH --status-dir PATH --clock-file PATH
--uplink unix:/run/ajin-edge/measurement.sock`. Independent asynchronous scan
subscriptions continue while a ten-envelope FIFO retries durable Enqueue with
the same ID and a two-second deadline. Overflow drops the oldest pending item
and counts the loss. Remote acknowledgements must echo the measurement ID.
Sensor subscription channels use `grpc.default_authority=localhost` so the
Python gRPC client interoperates with UDS servers that validate HTTP/2 authority.
The measurement uplink channel does not apply this sensor-only option.
Status is emitted every second and retains measurement reason codes. Invalid
calibration writes FATAL/CALIBRATION_INVALID before startup exits, replacing any
previous healthy instance snapshot. A supervisor should use status freshness for
process hangs; retries alone do not imply a hang.

Rapid rise/drop and suspected collection/discharge candidate hints are not
implemented. Defining and validating those requires a separate field model;
this pipeline emits section measurements and quality reasons, not confirmed
business events.
