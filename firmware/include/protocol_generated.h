/*
 * GENERATED FILE - DO NOT EDIT.
 *
 * Produced by server/tools/gen_protocol_header.py from
 * server/app/turret/protocol.py. Re-run the generator after changing the
 * protocol; `--check` fails the build if this file is stale.
 *
 * Prose specification: docs/PROTOCOL.md
 */

#pragma once

/* Wire protocol version. The server refuses to command a controller whose
 * version differs from its own. */
#define TURRET_PROTOCOL_VERSION 1

/* Largest accepted WebSocket frame, bytes. */
#define TURRET_MAX_FRAME_BYTES 16384

/* ---- server -> controller message types ---- */
#define MSG_HELLO_ACK     "hello_ack"
#define MSG_MOVE_ABSOLUTE "move_absolute"
#define MSG_MOVE_RELATIVE "move_relative"
#define MSG_JOG           "jog"
#define MSG_HOME          "home"
#define MSG_STOP          "stop"
#define MSG_SPRAY         "spray"
#define MSG_SPRAY_STOP    "spray_stop"
#define MSG_ARM_OUTPUT    "arm_output"
#define MSG_SET_CONFIG    "set_config"
#define MSG_GET_CONFIG    "get_config"
#define MSG_PING          "ping"
#define MSG_REBOOT        "reboot"

/* ---- controller -> server message types ---- */
#define MSG_HELLO  "hello"
#define MSG_STATUS "status"
#define MSG_ACK    "ack"
#define MSG_EVENT  "event"
#define MSG_PONG   "pong"
#define MSG_CONFIG "config"
#define MSG_LOG    "log"

/* ---- command rejection codes ---- */
#define ERR_NOT_HOMED     "NOT_HOMED"
#define ERR_LIMIT         "LIMIT"
#define ERR_DISARMED      "DISARMED"
#define ERR_ESTOP         "ESTOP"
#define ERR_INVALID_PARAM "INVALID_PARAM"
#define ERR_BUSY          "BUSY"
#define ERR_TIMEOUT       "TIMEOUT"
#define ERR_UNSUPPORTED   "UNSUPPORTED"
#define ERR_FAULT         "FAULT"

/* ---- controller states (reported in `status.state`) ---- */
#define STATE_BOOT    "BOOT"
#define STATE_IDLE    "IDLE"
#define STATE_MOVING  "MOVING"
#define STATE_HOMING  "HOMING"
#define STATE_JOGGING "JOGGING"
#define STATE_FAULT   "FAULT"
#define STATE_ESTOP   "ESTOP"

/* ---- asynchronous controller events ---- */
#define EVT_BOOT             "boot"
#define EVT_HOMING_STARTED   "homing_started"
#define EVT_HOMING_COMPLETED "homing_completed"
#define EVT_HOMING_FAILED    "homing_failed"
#define EVT_LIMIT_HIT        "limit_hit"
#define EVT_ESTOP            "estop"
#define EVT_ESTOP_CLEARED    "estop_cleared"
#define EVT_VALVE_OPENED     "valve_opened"
#define EVT_VALVE_CLOSED     "valve_closed"
#define EVT_WATCHDOG_RESET   "watchdog_reset"
#define EVT_CONFIG_SAVED     "config_saved"
#define EVT_FAULT            "fault"

/* ---- WebSocket close codes used by the server ---- */
#define TURRET_CLOSE_BAD_REQUEST      4400
#define TURRET_CLOSE_UNAUTHORIZED     4401
#define TURRET_CLOSE_REPLACED         4409
#define TURRET_CLOSE_VERSION_MISMATCH 4426
