#!/usr/bin/env node
// SPDX-License-Identifier: MPL-2.0

import { main } from "../lib/cli.js";

const code = await main();
process.exitCode = code;
