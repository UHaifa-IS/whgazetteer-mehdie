-- Find all customers in Berlin
SELECT * FROM Customers WHERE City='Berlin';

-- Find all customers in Mexico City
SELECT * FROM Customers WHERE City='México D.F.';

-- Find avg price of all products
SELECT AVG(Price) FROM Products

-- Find number of products that Have price = 18
SELECT * FROM Products WHERE Price=18;


-- Find orders between 1996-08-01 and 1996-09-06
SELECT * FROM Orders WHERE OrderDate BETWEEN #1996-08-01# AND #1996-09-06#;

-- Find customers with more than 3 orders
SELECT CustomerID,COUNT(*) TotalOrders  FROM Orders GROUP BY CustomerID HAVING TotalOrders >3;

-- Find all customers that are from the same city
-- question not so cleared to me



