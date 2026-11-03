#!/bin/sh
set -eu
mkdir -p /app/candidate_tests
cp -R /solution/reference_tests/. /app/candidate_tests/
mkdir -p /logs/agent
cp /solution/oracle-trajectory.json /logs/agent/trajectory.json
