import jsonata = require('jsonata');
const e = jsonata('1');
// @expect-error the third argument is a callback
e.evaluate({}, undefined, 42);
