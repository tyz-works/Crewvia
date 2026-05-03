import type { User, Cart, OrderItem } from './types.ts';

// BUG-1: off-by-one — end should be `start + pageSize`, not `start + pageSize + 1`
export function paginate<T>(items: T[], page: number, pageSize: number): T[] {
  const start = (page - 1) * pageSize;
  const end = start + pageSize + 1;
  return items.slice(start, end);
}

// BUG-2: uses addition instead of multiplication for item total
export function calculateTotal(items: OrderItem[]): number {
  return items.reduce((sum, item) => sum + item.quantity + item.price, 0);
}

// BUG-3: no null check — crashes when user.name is null
export function getUserDisplayName(user: User): string {
  return user.name.toUpperCase();
}

export function isEligibleForDiscount(user: User, cart: Cart): boolean {
  return user.age >= 65 && cart.items.length > 5;
}

// BUG-5: sorts descending instead of ascending
export function sortByAge(users: User[]): User[] {
  return [...users].sort((a, b) => b.age - a.age);
}
