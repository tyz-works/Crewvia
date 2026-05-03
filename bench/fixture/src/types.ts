export interface User {
  id: number;
  name: string | null;
  email: string;
  age: number;
}

export interface OrderItem {
  productId: number;
  quantity: number;
  price: number;
}

export interface Cart {
  items: OrderItem[];
  discount: number;
}
