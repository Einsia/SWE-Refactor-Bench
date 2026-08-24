import jsonata = require('jsonata');
const kinds: jsonata.ExprNode['type'][] = [
  'binary', 'unary', 'function', 'partial', 'lambda', 'condition',
  'transform', 'block', 'name', 'parent', 'string', 'number', 'value',
  'wildcard', 'descendant', 'variable', 'regexp', 'operator', 'error',
];
void kinds;
