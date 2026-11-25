// SPDX-License-Identifier: MIT
// Test-only ERC20 used by the integration tests. Can charge a transfer tax
// and can act as a "honeypot" (buys work, sells into the pair revert).
pragma solidity ^0.8.20;

contract TestToken {
    string public name;
    string public symbol;
    uint8 public decimals;
    uint256 public totalSupply;
    uint256 public taxBps;
    bool public honeypot;
    address public owner;
    address public pair;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory n, string memory s, uint8 d, uint256 supply, uint256 tax, bool hp) {
        name = n; symbol = s; decimals = d; taxBps = tax; honeypot = hp; owner = msg.sender;
        totalSupply = supply; balanceOf[msg.sender] = supply;
        emit Transfer(address(0), msg.sender, supply);
    }

    function setPair(address p) external { require(msg.sender == owner, "owner"); pair = p; }

    function approve(address s, uint256 v) external returns (bool) {
        allowance[msg.sender][s] = v; emit Approval(msg.sender, s, v); return true;
    }

    function transfer(address t, uint256 v) external returns (bool) { _transfer(msg.sender, t, v); return true; }

    function transferFrom(address f, address t, uint256 v) external returns (bool) {
        uint256 a = allowance[f][msg.sender];
        require(a >= v, "allowance");
        if (a != type(uint256).max) allowance[f][msg.sender] = a - v;
        _transfer(f, t, v);
        return true;
    }

    function _transfer(address f, address t, uint256 v) internal {
        require(balanceOf[f] >= v, "balance");
        if (honeypot && t == pair && f != owner) revert("honeypot");
        uint256 fee = (f == owner || t == owner) ? 0 : v * taxBps / 10000;
        balanceOf[f] -= v;
        balanceOf[t] += v - fee;
        balanceOf[owner] += fee;
        emit Transfer(f, t, v - fee);
    }
}
