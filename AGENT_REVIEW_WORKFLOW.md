# ClearVault multi-agent review workflow

This repository uses a short review cycle for changes that affect importing,
extraction, review, knowledge organisation, search, or model costs. It exists
to catch conflicts between a plausible design and the behaviour of real links.

## Before implementation

1. The product reviewer checks the proposed screen and flow against the user
   journey: import a saved video, understand it, decide whether to keep it,
   organise it, then find a direct answer later.
2. The reliability reviewer checks network failures, queue recovery, duplicate
   work, persistent data and platform limitations.
3. The model-quality reviewer checks input size, request count, evidence
   grounding, fallback behaviour and cost assumptions.

Each reviewer reports concrete risks, affected files, and an acceptance test.
Reviewers do not edit production code during this stage.

## Implementation and verification

1. The implementation changes only the accepted scope.
2. A verification pass runs automated tests and a realistic end-to-end sample.
3. The product reviewer inspects the resulting screen using a normal and a
   narrow desktop window.
4. The reliability reviewer checks one success case and one expected failure
   case, including retry or fallback behaviour.

## Release gate

Do not call a feature complete until it has been tried with three to five real
links from the same learning topic. Record the links privately, the processing
result, review decision, extracted structure, knowledge-node count, and at
least five search questions. Media, transcripts, API keys and personal
collections never enter the public repository.

## Current default roles

- Product and knowledge-structure reviewer
- Reliability and recovery reviewer
- Model quality, request load and cost reviewer
- Implementer and final verifier

For small copy-only changes, the implementation and verification steps may be
combined. For any change involving external platforms or a cloud model, keep
the separate review roles.
