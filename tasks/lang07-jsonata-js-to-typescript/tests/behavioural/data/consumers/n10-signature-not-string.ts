import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error the signature is a string
e.registerFunction('f', () => 1, 42);
