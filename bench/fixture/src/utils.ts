import type { User, Cart, OrderItem } from './types.ts';

export function paginate<T>(items: T[], page: number, pageSize: number): T[] {
  const start = (page - 1) * pageSize;
  const end = start + pageSize;
  return items.slice(start, end);
}

export function calculateTotal(items: OrderItem[]): number {
  return items.reduce((sum, item) => sum + item.quantity * item.price, 0);
}

export function getUserDisplayName(user: User): string {
  return user.name ? user.name.toUpperCase() : '';
}

export function isEligibleForDiscount(user: User, cart: Cart): boolean {
  return user.age >= 65 && cart.items.length > 5;
}

// BUG-5: sorts descending instead of ascending
export function sortByAge(users: User[]): User[] {
  return [...users].sort((a, b) => b.age - a.age);
}
