# Contributing to Oh My Prime

Oh My Prime accepts focused bug reports, design discussions, documentation improvements, tests, and code contributions. Correctness and verifiable behavior take priority over patch size or speed of merge.

## Before opening a pull request

1. Search existing issues and pull requests for overlapping work.
2. For a substantial feature, protocol change, security boundary, or architectural replacement, open an issue first and agree on the observable contract.
3. Read the [development guide](packages/coding-agent/docs/development.md) and repository rules in [`AGENTS.md`](AGENTS.md).
4. Keep the change focused. Do not combine unrelated cleanup with behavioral work.
5. Add or update tests when the change introduces an observable contract or fixes a reproducible bug.
6. Update the affected package changelog under `## [Unreleased]` for user-visible changes.
7. Run `npm run check`. If You changed a test file, run that focused test from its package root.

## Contributor License Agreement

External code and documentation contributions require acceptance of the [Oh My Prime Individual Contributor License Agreement](CLA.md). You retain copyright in Your Contribution while granting the Project Owner broad copyright and patent rights, including sublicensing and relicensing rights.

Post this exact statement as a comment on each contributor's first pull request:

> I have read and agree to the Oh My Prime Individual Contributor License Agreement, version 2026-08-11.

A checkbox is not sufficient by itself. A maintainer must confirm the durable acceptance record before merge. Contributions made on behalf of a company may require a separate corporate agreement.

## Licensing expectations

Accepted Contributions become part of the Project under the Project's GNU AGPLv3 license and [additional attribution terms](ADDITIONAL_TERMS.md), without limiting the broader rights granted to the Project Owner under the CLA.

Do not remove or obscure copyright, license, source-offer, attribution, provenance, or third-party notices. Do not submit code under terms that conflict with AGPLv3, the additional terms, or inherited third-party licenses.

## Pull request requirements

A pull request should contain:

- a concise statement of the problem and the intended observable result;
- the affected packages and public interfaces;
- the verification performed, with exact commands and outcomes;
- compatibility, security, migration, or performance implications where relevant;
- screenshots or terminal captures for user-interface changes; and
- a changelog entry when users will observe the change.

Reviewers may request a reproducer, acceptance contract, adversarial test, isolated ProofTree run, or verifier evidence for load-bearing changes.

## Review and merge policy

- Maintainers may close work that duplicates an accepted direction, weakens a security or verification invariant, or cannot be maintained safely.
- Passing CI is necessary but does not guarantee merge.
- The Project Owner decides whether a Contribution fits the product direction and may edit, squash, or decline it.
- No pull request is merged automatically, and approval does not transfer trademark rights or imply endorsement.

## Attribution

The Project records authorship through Git history and release notes where appropriate. The required downstream Oh My Prime attribution is defined in [`ADDITIONAL_TERMS.md`](ADDITIONAL_TERMS.md); the trademark policy is in [`TRADEMARKS.md`](TRADEMARKS.md).
