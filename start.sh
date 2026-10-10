#!/bin/bash

# Exit immediately if a command exits with a non-zero status
set -e 

if [ -z "$UPSTREAM_REPO" ]
then
  echo "Cloning main Repository..."
  git clone https://github.com/UNKOWN5757H/Autonewtest /Autonewtest
else
  echo "Cloning Custom Repo from $UPSTREAM_REPO..."
  git clone "$UPSTREAM_REPO" /Autonewtest
fi

cd /Autonewtest

echo "Installing Dependencies..."
pip3 install --no-cache-dir -U -r requirements.txt

echo "Starting Bot and Web Server..."
python3 bot.py
