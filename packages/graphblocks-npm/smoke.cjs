"use strict";

const assert = require("node:assert/strict");

async function main() {
  const thrownErrors = [];
  try {
    require(".");
  } catch (error) {
    thrownErrors.push(error);
  }
  try {
    await import("graphblocks");
  } catch (error) {
    thrownErrors.push(error);
  }

  assert.equal(
    thrownErrors.length,
    2,
    "requiring and importing the reserved package must fail",
  );
  for (const thrown of thrownErrors) {
    assert.ok(thrown instanceof Error);
    assert.equal(thrown.name, "GraphBlocksReservedPackageError");
    assert.equal(thrown.code, "ERR_GRAPHBLOCKS_RESERVED_PACKAGE");
    assert.match(thrown.message, /contains no JavaScript or TypeScript API/);
    assert.match(thrown.message, /https:\/\/pypi\.org\/project\/graphblocks\//);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
