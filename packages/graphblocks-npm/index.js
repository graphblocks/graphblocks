"use strict";

const error = new Error(
  "The npm package `graphblocks` is a reserved name and contains no " +
    "JavaScript or TypeScript API. Do not depend on it. The supported " +
    "GraphBlocks distribution is the Python package at " +
    "https://pypi.org/project/graphblocks/.",
);
error.name = "GraphBlocksReservedPackageError";
error.code = "ERR_GRAPHBLOCKS_RESERVED_PACKAGE";
throw error;
