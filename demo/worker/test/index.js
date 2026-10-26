// `node --test test/` hands a directory to Node's runner, which resolves it as a module (this file)
// instead of expanding it. Importing the test files here makes that command work.
import "./worker.test.js";
