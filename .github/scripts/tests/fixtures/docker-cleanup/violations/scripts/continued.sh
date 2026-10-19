#!/usr/bin/env bash
sudo docker container prune \
  --force \
  --filter "until=24h"
