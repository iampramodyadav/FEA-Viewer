from rst_reader import read_rst

with read_rst('file.rst') as rst:
    print(rst)
    print()
    print(rst.summary())
    print()

    for rnum in range(rst.n_results):
        nnum, disp = rst.nodal_solution(rnum)
        _, mag = rst.displacement_magnitude(rnum)
        ls = rst.ls_table[rnum]
        print(f"Result {rnum:2d}  LS={ls['loadstep']} SS={ls['substep']:2d}  "
              f"nodes={len(nnum)}  max|U|={mag.max():.4f}")

    print()
    nnum_all, all_disp = rst.nodal_solution_all()
    print(f"All results array: {all_disp.shape}  "
          f"(n_results × n_nodes × 3_dof)")
    print()
    print("Node mapping (first 10 nodes, result 0):")
    nnum0, d0 = rst.nodal_solution(0)
    for i in range(min(10, len(nnum0))):
        print(f"  Node {nnum0[i]:4d}:  "
              f"UX={d0[i,0]:10.4f}  UY={d0[i,1]:10.4f}  UZ={d0[i,2]:10.4f}")