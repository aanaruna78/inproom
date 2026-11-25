package com.acme.orders;

import java.util.List;
import static java.util.Objects.requireNonNull;
import com.acme.common.*;

/** Orders. class Fake {} in a comment */
@Service
public final class OrderService implements Orders {
    public static final int MAX_ITEMS = 50;
    private final Repo repo;

    public OrderService(Repo repo) {
        this.repo = requireNonNull(repo);
    }

    @Override
    public List<Order> find(String customer) throws NotFound {
        if (customer == null) {
            throw new NotFound("x");
        }
        return repo.find(customer);
    }

    private static <T> T first(List<T> items) {
        return items.get(0);
    }

    abstract void hook();

    public enum Status { OPEN, CLOSED }

    static class Line {
        int qty() { return 1; }
    }
}

interface Orders {
    List<Order> find(String customer);
}

record Money(long cents, String currency) {}
