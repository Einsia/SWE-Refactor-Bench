import jsonata = require('jsonata');
const node = jsonata('1').ast();
// @expect-error ExprNode has no such property
void node.nosuchfield;
