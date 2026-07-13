#!/usr/bin/env bash
set -e
systemctl --user restart hermes-gateway-jarvis-voice.service 2>&1
sleep 2
systemctl --user is-active hermes-gateway-jarvis-voice.service 2>&1
exit 0
