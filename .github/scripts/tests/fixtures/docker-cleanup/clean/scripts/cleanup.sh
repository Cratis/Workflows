#!/usr/bin/env bash
docker ps -aq --filter "label=run=$RUN" | xargs -r docker rm -f
docker rm -f named-container # docker kill $(docker ps -q)
