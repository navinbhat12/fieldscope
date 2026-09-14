"""Spark session configured for distributed spatial work.

Sedona ships its JVM side as Maven artifacts rather than inside the Python
wheel, so they are declared here and resolved on first run (cached in
~/.ivy2 afterwards). The artifact coordinate encodes the Spark version --
sedona-spark-shaded-3.5 is not interchangeable with a 3.4 or 4.0 build, which
is why the runtime is pinned in pyproject.toml.
"""

import os
import sys

from sedona.spark import SedonaContext

SEDONA_VERSION = "1.7.1"
SPARK_SERIES = "3.5"
SCALA = "2.12"

PACKAGES = ",".join(
    [
        f"org.apache.sedona:sedona-spark-shaded-{SPARK_SERIES}_{SCALA}:{SEDONA_VERSION}",
        "org.datasyslab:geotools-wrapper:1.7.1-28.5",
    ]
)


def build(
    app_name: str = "fieldscope",
    local_threads: str = "*",
    memory: str = "8g",
    conf: dict[str, str] | None = None,
):
    """Create a Sedona-enabled local Spark session.

    Runs local[*] deliberately. The pipeline is a batch job over a bounded
    dataset, and a local multi-core session exercises exactly the same
    execution model as a cluster -- partitioned data, a spatial partitioner,
    and a distributed join -- without the operational cost of running one.
    Scaling out means changing the master URL, not the job.
    """
    os.environ.setdefault("JAVA_HOME", "/opt/homebrew/opt/openjdk@17")

    # Spark launches Python workers with whatever `python3` is on PATH, which
    # is the system 3.9 here rather than the venv's 3.11, and it refuses to run
    # across minor versions. Nothing needed a worker while every operation was
    # a JVM-side Sedona expression, so this stayed invisible until the first
    # one did -- point both at the interpreter actually running this process.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)

    builder = (
        SedonaContext.builder()
        .appName(app_name)
        .master(f"local[{local_threads}]")
        .config("spark.jars.packages", PACKAGES)
        .config("spark.driver.memory", memory)
        # Kryo is required for Sedona: geometries are serialized between
        # stages constantly and Java serialization is prohibitively slow.
        .config("spark.serializer", "org.apache.spark.serializer.KryoSerializer")
        .config("spark.kryo.registrator", "org.apache.sedona.core.serde.SedonaKryoRegistrator")
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.parquet.compression.codec", "zstd")
        # Quieter shutdown; the local session logs noisily on stop otherwise.
        .config("spark.ui.showConsoleProgress", "false")
    )

    # Join-strategy settings are passed in rather than fixed here, because the
    # right strategy depends on how much geometry the job actually has -- see
    # the strategy selection in scripts/run_join.py.
    for key, value in (conf or {}).items():
        builder = builder.config(key, value)

    spark = SedonaContext.create(builder.getOrCreate())
    spark.sparkContext.setLogLevel("WARN")
    return spark
