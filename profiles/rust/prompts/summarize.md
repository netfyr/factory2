You are generating a final summary of a software factory run.

Review all story results and the implemented project. Write a summary to the output file path given below.

Structure:

## Overview
Brief description of what was built. What does this project do?

## Stories Implemented
For each completed story: one-line summary of what was built and whether tests pass.

## Stories Quarantined
For each quarantined story: what went wrong, and the error or failure reason.

## Stories Skipped
For each skipped story: which dependency failed, causing it to be skipped.

## Project Status
Run `cargo test` to confirm current state. Does the project compile? Do all tests pass? Report the numbers.

## Architecture Notes
Brief description of the project structure: key modules, types, and how they fit together. This is for someone picking up the project for the first time.
