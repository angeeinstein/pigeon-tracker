# Component definition for the application.
#
# PlatformIO generates this file if it is missing, but its version globs
# `src/*.*` — which sweeps idf_component.yml in as a "source". Listing the
# translation units explicitly is also a useful tripwire: a new .c file that
# nobody added here fails to link rather than silently doing nothing.

idf_component_register(
    SRCS
        "main.c"
        "motion.c"
        "turret_config.c"
        "valve.c"
        "wifi_manager.c"
        "ws_client.c"
    INCLUDE_DIRS
        "."
        "../include"
)
