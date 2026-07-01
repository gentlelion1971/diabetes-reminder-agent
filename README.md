# Diabetes Nightscout Agent

A local Python agent that monitors a Nightscout site and sends email reminders for diabetes-related events.

## What it monitors

- Current CGM value and stale data
- Loop prediction for possible low/high glucose
- Possible missed bolus based on steady BG rise with no carb/bolus record in the previous hour
- Pod age based on Nightscout `Site Change / Pod Change` treatments
- Dexcom age based on local `dexcomage.txt`
- Estimated pod insulin remaining using initial fill minus delivered insulin

## Safety boundary

This tool does **not** recommend insulin dosing. It only sends reminders to check Dexcom/Loop and follow the existing care plan.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
adapt the content to your configuration

cp podage.example.txt podage.txt
cp dexcomage.example.txt dexcomage.txt
