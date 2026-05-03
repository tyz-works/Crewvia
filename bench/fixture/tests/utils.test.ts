import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { paginate, calculateTotal, getUserDisplayName, isEligibleForDiscount, sortByAge } from '../src/utils.ts';
import type { User, OrderItem, Cart } from '../src/types.ts';

describe('paginate', () => {
  test('first page returns exactly pageSize items', () => {
    const items = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    assert.deepEqual(paginate(items, 1, 3), [1, 2, 3]);
  });

  test('second page returns correct items', () => {
    const items = [1, 2, 3, 4, 5, 6];
    assert.deepEqual(paginate(items, 2, 2), [3, 4]);
  });
});

describe('calculateTotal', () => {
  test('multiplies quantity by price for each item', () => {
    const items: OrderItem[] = [
      { productId: 1, quantity: 2, price: 10 },
      { productId: 2, quantity: 3, price: 5 },
    ];
    assert.equal(calculateTotal(items), 35); // 2*10 + 3*5
  });

  test('returns 0 for empty cart', () => {
    assert.equal(calculateTotal([]), 0);
  });
});

describe('getUserDisplayName', () => {
  test('returns uppercased name for valid user', () => {
    const user: User = { id: 1, name: 'Alice', email: 'alice@example.com', age: 30 };
    assert.equal(getUserDisplayName(user), 'ALICE');
  });

  test('returns empty string when name is null', () => {
    const user: User = { id: 2, name: null, email: 'test@example.com', age: 25 };
    assert.equal(getUserDisplayName(user), '');
  });
});

describe('isEligibleForDiscount', () => {
  test('senior citizen with large cart is eligible', () => {
    const user: User = { id: 1, name: 'Bob', email: 'bob@example.com', age: 70 };
    const cart: Cart = {
      items: Array(6).fill({ productId: 1, quantity: 1, price: 10 }),
      discount: 0,
    };
    assert.equal(isEligibleForDiscount(user, cart), true);
  });

  test('young user with large cart is not eligible', () => {
    const user: User = { id: 2, name: 'Alice', email: 'alice@example.com', age: 30 };
    const cart: Cart = {
      items: Array(6).fill({ productId: 1, quantity: 1, price: 10 }),
      discount: 0,
    };
    assert.equal(isEligibleForDiscount(user, cart), false);
  });

  test('senior citizen with small cart is not eligible', () => {
    const user: User = { id: 3, name: 'Charlie', email: 'charlie@example.com', age: 70 };
    const cart: Cart = {
      items: Array(3).fill({ productId: 1, quantity: 1, price: 10 }),
      discount: 0,
    };
    assert.equal(isEligibleForDiscount(user, cart), false);
  });
});

describe('sortByAge', () => {
  test('sorts users by age ascending', () => {
    const users: User[] = [
      { id: 1, name: 'Alice', email: 'a@example.com', age: 30 },
      { id: 2, name: 'Bob', email: 'b@example.com', age: 25 },
      { id: 3, name: 'Charlie', email: 'c@example.com', age: 40 },
    ];
    const sorted = sortByAge(users);
    assert.equal(sorted[0].age, 25);
    assert.equal(sorted[1].age, 30);
    assert.equal(sorted[2].age, 40);
  });
});
