# Manual Metrics Jobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `manual-metrics-jobs` config option that renders operator-defined Alloy scrape jobs for non-related targets.

**Architecture:** Parse the YAML config in a dedicated helper module, translate valid entries into the existing `MetricsScrapeJob` model, and merge those jobs with relation-derived scrape jobs before rendering the managed Alloy config. Keep remote-write gating and explicit validation behavior in the charm.

**Tech Stack:** Python, PyYAML, ops, pytest

---
