#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Adam Townsend, adam@adamtownsend.com, 07/06/2017

import numpy as np
import numba
import time
import datetime
from numpy import linalg
from functions_generate_grand_resistance_matrix import (
    generate_grand_resistance_matrix, generate_grand_resistance_matrix_periodic)
from functions_shared import (posdata_data, format_elapsed_time, throw_error,
                              shear_basis_vectors)
from functions_simulation_tools import (
    construct_force_vector_from_fts, deconstruct_velocity_vector_for_fts,
    fts_to_fte_matrix, fte_to_ufte_matrix, ufte_to_ufteu_matrix,
    fts_to_duf_matrix)
from input_setups import input_ftsuoe


def euler_timestep(x, u, timestep):
    """Returns the next Euler timestep, `x + timestep * u`."""
    return (x + timestep * u).astype('float')


def ab2_timestep(x, u, u_previous, timestep):
    """Returns the next Adams-Bashforth 2 timestep."""
    return x + timestep * (1.5 * u - 0.5 * u_previous)


def did_something_go_wrong_with_dumbells(error, dumbbell_deltax,
                                         new_dumbbell_deltax,
                                         explosion_protection):
    """Check new dumbbell lengths for signs of numerical explosion.

    Specifically, see whether dumbbells have rotated or stretched too far in a timestep.

    Args:
        error (bool): Current error flag from any previous error checks.
        dumbbell_deltax: Existing dumbbell displacements.
        new_dumbbell_deltax: New dumbbell displacements after timestep.
        explosion_protection (bool): Flag whether to enact the length check.

    Returns:
        error: True if these checks flag up problems, else passes through inputted value."""

    for i in range(new_dumbbell_deltax.shape[0]):
        if np.arccos(np.round(np.dot(dumbbell_deltax[i], new_dumbbell_deltax[i]) / (np.linalg.norm(dumbbell_deltax[i]) * np.linalg.norm(new_dumbbell_deltax[i])), 4)) > np.pi / 2:
            print(" ")
            print(f"Code point H# reached on dumbbell {str(i)}")
            print(f"Old delta x: {str(dumbbell_deltax[i])}")
            print(f"New delta x: {str(new_dumbbell_deltax[i])}")
        if explosion_protection and np.linalg.norm(new_dumbbell_deltax[i]) > 5:
            print("ERROR")
            print(
                f"Dumbbell {str(i)} length ({str(np.linalg.norm(new_dumbbell_deltax[i]))}) has exceeded 5."
            )
            print("Something has probably gone wrong (normally your timestep is too large).")
            print("Code exiting gracefully.")
            error = True
    return error


def euler_timestep_rotation(sphere_positions, sphere_rotations,
                            new_sphere_positions, new_sphere_rotations,
                            Oa_out, timestep):
    """Returns new rotation vectors after an Euler timestep using Oa_out as the
    velocity.

    See comments inside the function for details."""

    for i in range(sphere_positions.shape[0]):
        R0 = sphere_positions[i]
        O = (Oa_out[i][0] ** 2 + Oa_out[i][1] ** 2 + Oa_out[i][2] ** 2) ** 0.5

        ''' To rotate from basis (x,y,z) to (X,Y,Z), where x,y,z,X,Y,Z are unit
        vectors, you just need to multiply by the matrix
        ( X_x   Y_x   Z_x )
        ( X_y   Y_y   Z_y ),
        ( X_z   Y_z   Z_z )
        where X_x means the x-component of X.
        Our Z is Omega = o_spheres[i], so we need to make it into a complete
        basis. To do that we pick a unit vector different to Omega (either zhat
        or xhat depending on Omega) and use
        (Omega x zhat, Omega x (Omega x zhat), zhat) as our basis (X,Y,Z).
        That's it! [Only took me three days...]
        '''

        if np.array_equal(Oa_out[i], [0, 0, 0]):
            rot_matrix = np.identity(3)
        else:
            Otest = (abs(Oa_out[i] / O)).astype('float')
            perp1 = [0, 0, 1] if np.allclose(Otest, [1, 0, 0]) else [1, 0, 0]
            rot_matrix = np.array([np.cross(Oa_out[i], perp1) / O,
                                   np.cross(Oa_out[i], np.cross(Oa_out[i], perp1)) / O ** 2,
                                   Oa_out[i] / O]
                                  ).transpose()

        for j in range(2):
            ''' rb0 is the position ("r") of the endpoint of the pointy
            rotation vector in the external (x,y,z) frame ("b") at the
            beginning of this process ("0") '''
            rb0 = sphere_rotations[i, j]

            ''' rbdashdash0_xyz is the position of the same endpoint in the
            frame of the rotating sphere ("b''"), which we set to have the
            z-axis=Omega axis. It's in Cartesian coordinates. '''
            rbdashdash0_xyz = np.dot(linalg.inv(rot_matrix), (rb0 - R0))
            x0 = rbdashdash0_xyz[0]
            y0 = rbdashdash0_xyz[1]
            z0 = rbdashdash0_xyz[2]

            r0 = (x0 ** 2 + y0 ** 2 + z0 ** 2) ** 0.5
            t0 = np.arccos(z0 / r0)
            p0 = 0 if (x0 == 0 and y0 == 0) else np.arctan2(y0, x0)
            r = r0
            t = t0
            p = euler_timestep(p0, O, timestep)

            x = r * np.sin(t) * np.cos(p)
            y = r * np.sin(t) * np.sin(p)
            z = r * np.cos(t)
            rbdashdash_xyz = np.array([x, y, z])
            R = new_sphere_positions[i]
            rb = R + np.dot(rot_matrix, rbdashdash_xyz)
            new_sphere_rotations[i, j] = rb
    return new_sphere_rotations


def ab2_timestep_rotation(sphere_positions, sphere_rotations,
                          new_sphere_positions, new_sphere_rotations,
                          Oa_out, Oa_out_previous, timestep):
    """Returns new rotation vectors after an Adams-Bashforth 2 timestep using
    Oa_out as the velocity.

    AB2 = Euler (x_n + u_n * dt) but with u_n replaced by (1.5 u_n - 0.5 u_{n-1})
    """
    combined_Oa_for_ab2 = 1.5*Oa_out - 0.5*Oa_out_previous
    return euler_timestep_rotation(sphere_positions, sphere_rotations,
                                   new_sphere_positions, new_sphere_rotations,
                                   combined_Oa_for_ab2, timestep)


def do_we_have_all_size_ratios(error, element_sizes, lam_range, num_spheres):
    """Check whether we have all particle size ratios in the R2Bexact lookup."""

    lambda_matrix = element_sizes / element_sizes[:, None]
    lam_range_including_reciprocals = np.concatenate([lam_range, 1 / lam_range])
    do_we_have_it_matrix = np.in1d(
        lambda_matrix, lam_range_including_reciprocals).reshape(
        [len(element_sizes), len(element_sizes)])
    offending_elements = np.where(do_we_have_it_matrix == 0)
    if len(offending_elements[0]) > 0:
        offending_lambda_1 = lambda_matrix[offending_elements[0][0], offending_elements[0][1]]
        offending_element_str = np.empty(2, dtype='|S25')
        for i in (0, 1):
            offending_element_str[i] = (
                f"dumbbell {str(offending_elements[0][i] - num_spheres)}"
                if offending_elements[0][i] >= num_spheres
                else f"sphere {str(offending_elements[0][i])}"
            )
        print("ERROR")
        print(
            f"Element size ratio ({str(offending_lambda_1)} or {str(1 / offending_lambda_1)}) is not in our calculated list of size ratios"
        )
        print(
            f"Offending elements: {offending_element_str[0]} and {offending_element_str[1]} (counting from 0)"
        )
        return True
    return error


def are_some_of_the_particles_too_close(error, printout, s_dash_range,
                                        sphere_positions, dumbbell_positions,
                                        dumbbell_deltax, sphere_sizes,
                                        dumbbell_sizes, element_positions):
    """Customise me: A function you can adapt to check if particles are too close.

    By default, just returns the value of the `error` flag given to it."""

    # Error check 1 : are some of my particles too close together for R2Bexact?
    # min_s_dash_range = np.min(s_dash_range)  # This is the minimum s' we have calculated values for

    sphere_and_bead_positions = np.concatenate([sphere_positions,
                                                dumbbell_positions + 0.5 * dumbbell_deltax,
                                                dumbbell_positions - 0.5 * dumbbell_deltax])
    sphere_and_bead_sizes = np.concatenate([sphere_sizes, dumbbell_sizes, dumbbell_sizes])
    sphere_and_bead_positions = sphere_and_bead_positions.astype('float')
    distance_matrix = np.linalg.norm(sphere_and_bead_positions - sphere_and_bead_positions[:, None], axis=2)
    average_size = 0.5 * (sphere_and_bead_sizes + sphere_and_bead_sizes[:, None])
    distance_over_average_size = distance_matrix / average_size  # Matrix of s'

    # min_element_distance = np.min(distance_over_average_size[np.nonzero(distance_over_average_size)])
    # two_closest_elements = np.where(distance_over_average_size == min_element_distance)

    if printout > 0:
        print("")
        print("Positions")
        print(np.array_str(element_positions, max_line_width=100000, precision=5))
        print("Dumbbell Delta x")
        print(np.array_str(dumbbell_deltax, max_line_width=100000, precision=5))
        print("Separation distances (s)")
        print(np.array_str(distance_matrix, max_line_width=100000, precision=3))
        print("Scaled Separation distances (s')")
        print(np.array_str(distance_over_average_size, max_line_width=100000, precision=3))

    return error


def generate_output_FTSUOE(
        posdata, frameno, timestep, input_number,
        last_generated_Minfinity_inverse, regenerate_Minfinity, input_form,
        cutoff_factor, printout, use_drag_Minfinity, use_Minfinity_only,
        extract_force_on_wall_due_to_dumbbells, last_velocities,
        last_velocity_vector, box_bottom_left, box_top_right,
        feed_every_n_timesteps=0):
    """Solve the grand mobility problem: for given force/velocity inputs,
    return all computed velocities/forces.

    Args (selected):
        posdata: Contains position, size and count data for all particles.
        input_number: Index of the force/velocity inputs, listed in
            `input_setups.py`.

    Returns:
        All force and velocity data, including both that given as inputs and
        that computed by solving the grand mobility problem.
    """

    (sphere_sizes, sphere_positions, sphere_rotations, dumbbell_sizes,
     dumbbell_positions, dumbbell_deltax, num_spheres, num_dumbbells,
     element_sizes, element_positions, element_deltax, num_elements,
     num_elements_array, element_type, uv_start, uv_size,
     element_start_count) = posdata_data(posdata)
    # Get inputs first time in "skip_computation" mode, i.e. no complex
    # calculations for Fa_in, etc. This is really just to get the values of
    # box_bottom_left and box_top_right.
    (Fa_in, Ta_in, Sa_in, Sa_c_in, Fb_in, DFb_in,
     Ua_in, Oa_in, Ea_in, Ea_c_in, Ub_in, HalfDUb_in, input_description,
     U_infinity, O_infinity, centre_of_background_flow, Ot_infinity,
     Et_infinity, box_bottom_left, box_top_right, mu) = input_ftsuoe(
        input_number, posdata, frameno, timestep, last_velocities,
        input_form=input_form, skip_computation=True)

    force_on_wall_due_to_dumbbells = 0

    if input_form == "stokes_drag_dumbbells_only":
        solve_time_start = time.time()
        Fbeads = 0.5*np.concatenate([np.array(Fb_in) + np.array(DFb_in),
                                     np.array(Fb_in) - np.array(DFb_in)])
        a = dumbbell_sizes[0]
        drag_coeff = mu*a
        Ubeads = Fbeads/drag_coeff
        Nbeads = len(Fbeads)
        (Fa_out, Ta_out, Sa_out, Fb_out, DFb_out) = (
            Fa_in[:], Ta_in[:], Sa_in[:], Fb_in[:], DFb_in[:])
        (Ua_out, Oa_out, Ea_out) = (Fa_in[:], Fa_in[:], Ea_in[:])
        Ub_out = 0.5*(Ubeads[:Nbeads/2] + Ubeads[Nbeads/2:])
        HalfDUb_out = 0.5*(Ubeads[:Nbeads/2] - Ubeads[Nbeads/2:])
        gen_times = [0, 0, 0]

    else:
        if not np.array_equal(box_bottom_left - box_top_right, np.array([0, 0, 0])):
            # periodic
            (grand_resistance_matrix, heading, last_generated_Minfinity_inverse,
                gen_times) = generate_grand_resistance_matrix_periodic(
                posdata,
                last_generated_Minfinity_inverse,
                box_bottom_left, box_top_right,
                regenerate_Minfinity=regenerate_Minfinity,
                cutoff_factor=cutoff_factor, printout=printout,
                use_drag_Minfinity=use_drag_Minfinity,
                use_Minfinity_only=use_Minfinity_only, frameno=frameno, mu=mu,
                O_infinity=O_infinity, E_infinity=Ea_in[0],
                timestep=timestep,
                centre_of_background_flow=centre_of_background_flow,
                Ot_infinity=Ot_infinity, Et_infinity=Et_infinity)
        else:
            # non-periodic
            (grand_resistance_matrix, heading, last_generated_Minfinity_inverse,
                gen_times) = generate_grand_resistance_matrix(
                posdata,
                last_generated_Minfinity_inverse,
                regenerate_Minfinity=regenerate_Minfinity,
                cutoff_factor=cutoff_factor, printout=printout,
                use_drag_Minfinity=use_drag_Minfinity,
                use_Minfinity_only=use_Minfinity_only, frameno=frameno, mu=mu)

        solve_time_start = time.time()

        num_spheres = len(Ua_in)
        num_dumbbells = len(Ub_in)
        if input_form == 'fts':
            try:
                force_vector = construct_force_vector_from_fts(posdata, Fa_in, Ta_in, Sa_in, Fb_in, DFb_in)
            except:
                throw_error("FTS mode has been selected but not all values of F, T and S have been provided.")
            velocity_vector = np.linalg.solve(grand_resistance_matrix, force_vector)
            (Ua_out, Oa_out, Ea_out, Ub_out, HalfDUb_out) = deconstruct_velocity_vector_for_fts(posdata, velocity_vector)
            (Fa_out, Ta_out, Sa_out, Fb_out, DFb_out) = (Fa_in[:], Ta_in[:], Sa_in[:], Fb_in[:], DFb_in[:])
            if num_spheres == 0:
                Ea_out = Ea_in

        elif input_form == 'fte':
            # Call this the same name to reduce memory requirements (no need to reproduce)
            grand_resistance_matrix = fts_to_fte_matrix(posdata, grand_resistance_matrix)

            # Get inputs a second time not in "skip_computation" mode, putting
            # in the grand resistance matrix which is needed for some
            # calculations with friction.
            (Fa_in, Ta_in, Sa_in, Sa_c_in, Fb_in, DFb_in,
             Ua_in, Oa_in, Ea_in, Ea_c_in, Ub_in, HalfDUb_in,
             input_description, U_infinity, O_infinity,
             centre_of_background_flow, Ot_infinity, Et_infinity,
             box_bottom_left, box_top_right, mu) = input_ftsuoe(
                input_number, posdata, frameno, timestep, last_velocities,
                input_form=input_form,
                grand_resistance_matrix_fte=grand_resistance_matrix)
            try:
                force_vector = construct_force_vector_from_fts(
                    posdata, Fa_in, Ta_in, Ea_in, Fb_in, DFb_in)
            except:
                throw_error("FTE mode has been selected but not all values of F, T and E have been provided.")
            velocity_vector = np.linalg.solve(grand_resistance_matrix,
                                              force_vector)
            (Ua_out, Oa_out, Sa_out, Ub_out, HalfDUb_out) = deconstruct_velocity_vector_for_fts(
                posdata, velocity_vector)
            (Fa_out, Ta_out, Ea_out, Fb_out, DFb_out) = (
                Fa_in[:], Ta_in[:], Ea_in[:], Fb_in[:], DFb_in[:])
            if num_spheres == 0:
                Ea_out = Ea_in

        elif input_form == 'ufte':
            num_fixed_velocity_spheres = num_spheres - Ua_in.count(['pippa', 'pippa', 'pippa'])
            try:
                force_vector = construct_force_vector_from_fts(
                    posdata,
                    Ua_in[0:num_fixed_velocity_spheres] + Fa_in[num_fixed_velocity_spheres:num_spheres],
                    Ta_in, Ea_in, Fb_in, DFb_in)
            except:
                throw_error("UFTE mode has been selected but not enough values of U, F, T and E have been provided. At a guess, not all your spheres have either a U or an F.")
            force_vector = np.array(force_vector, float)
            grand_resistance_matrix_fte = fts_to_fte_matrix(
                posdata, grand_resistance_matrix)
            grand_resistance_matrix_ufte = fte_to_ufte_matrix(
                num_fixed_velocity_spheres, posdata, grand_resistance_matrix_fte)
            if extract_force_on_wall_due_to_dumbbells:
                grand_mobility_matrix_ufte = np.linalg.inv(grand_resistance_matrix_ufte)
                velocity_vector = np.dot(grand_mobility_matrix_ufte, force_vector)
            else:
                velocity_vector = np.linalg.solve(grand_resistance_matrix_ufte, force_vector)
                (FUa_out, Oa_out, Sa_out, Ub_out, HalfDUb_out) = deconstruct_velocity_vector_for_fts(posdata, velocity_vector)
                Fa_out = [['chen', 'chen', 'chen'] for i in range(num_spheres)]
                Ua_out = [['chen', 'chen', 'chen'] for i in range(num_spheres)]
                Fa_out[0:num_fixed_velocity_spheres] = FUa_out[0:num_fixed_velocity_spheres]
                Fa_out[num_fixed_velocity_spheres:num_spheres] = Fa_in[num_fixed_velocity_spheres:num_spheres]
                Ua_out[0:num_fixed_velocity_spheres] = Ua_in[0:num_fixed_velocity_spheres]
                Ua_out[num_fixed_velocity_spheres:num_spheres] = FUa_out[num_fixed_velocity_spheres:num_spheres]
                (Ta_out, Ea_out, Fb_out, DFb_out) = (Ta_in[:], Ea_in[:], Fb_in[:], DFb_in[:])

            if extract_force_on_wall_due_to_dumbbells:
                # For finding effect of the dumbbells on the measured Force on the walls.
                # Since   Fafixed = ()Uafixed + ()Fafree + ()Ta + ()E +   ()Fb  + ()DFb       ,
                #                                                       | want this bit |
                force_on_wall_due_to_dumbbells_matrix = grand_mobility_matrix_ufte[:num_fixed_velocity_spheres*3, 11*num_spheres:]
                dumbbell_forces = force_vector[11*num_spheres:]
                force_on_wall_due_to_dumbbells_flat = np.dot(force_on_wall_due_to_dumbbells_matrix, dumbbell_forces)
                force_on_wall_due_to_dumbbells = force_on_wall_due_to_dumbbells_flat.reshape(len(force_on_wall_due_to_dumbbells_flat)/3, 3)

        elif input_form == 'ufteu':
            num_fixed_velocity_spheres = num_spheres - Ua_in.count(['pippa', 'pippa', 'pippa'])
            num_fixed_velocity_dumbbells = num_dumbbells - Ub_in.count(['pippa', 'pippa', 'pippa'])
            try:
                force_vector = construct_force_vector_from_fts(
                    posdata,
                    Ua_in[0:num_fixed_velocity_spheres] + Fa_in[num_fixed_velocity_spheres:num_spheres],
                    Ta_in, Ea_in,
                    Ub_in[0:num_fixed_velocity_dumbbells] + Fb_in[num_fixed_velocity_dumbbells:num_dumbbells],
                    HalfDUb_in[0:num_fixed_velocity_dumbbells] + DFb_in[num_fixed_velocity_dumbbells:num_dumbbells])
            except:
                throw_error("UFTEU mode has been selected but not enough values of U, F, T and E and U(dumbbell) have been provided. At a guess, not all your spheres/dumbbells have either a U or an F.")

            force_vector = np.array(force_vector, float)
            grand_resistance_matrix_fte = fts_to_fte_matrix(
                posdata, grand_resistance_matrix)
            grand_resistance_matrix_ufte = fte_to_ufte_matrix(
                num_fixed_velocity_spheres, posdata, grand_resistance_matrix_fte)
            grand_resistance_matrix_ufteu = ufte_to_ufteu_matrix(
                num_fixed_velocity_dumbbells, num_fixed_velocity_spheres,
                posdata, grand_resistance_matrix_ufte)
            velocity_vector = np.linalg.solve(grand_resistance_matrix_ufteu,
                                              force_vector)

            (FUa_out, Oa_out, Sa_out, FUb_out, DFUb_out) = deconstruct_velocity_vector_for_fts(posdata, velocity_vector)
            Fa_out = [['chen', 'chen', 'chen'] for i in range(num_spheres)]
            Ua_out = [['chen', 'chen', 'chen'] for i in range(num_spheres)]
            Fa_out[0:num_fixed_velocity_spheres] = FUa_out[0:num_fixed_velocity_spheres]
            Fa_out[num_fixed_velocity_spheres:num_spheres] = Fa_in[num_fixed_velocity_spheres:num_spheres]
            Ua_out[0:num_fixed_velocity_spheres] = Ua_in[0:num_fixed_velocity_spheres]
            Ua_out[num_fixed_velocity_spheres:num_spheres] = FUa_out[num_fixed_velocity_spheres:num_spheres]
            (Ta_out, Ea_out) = (Ta_in[:], Ea_in[:])
            Fb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            Ub_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            Fb_out[0:num_fixed_velocity_dumbbells] = FUb_out[0:num_fixed_velocity_dumbbells]
            Fb_out[num_fixed_velocity_dumbbells:num_dumbbells] = Fb_in[num_fixed_velocity_dumbbells:num_dumbbells]
            Ub_out[0:num_fixed_velocity_dumbbells] = Ub_in[0:num_fixed_velocity_dumbbells]
            Ub_out[num_fixed_velocity_dumbbells:num_dumbbells] = FUb_out[num_fixed_velocity_dumbbells:num_dumbbells]
            DFb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            HalfDUb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            DFb_out[0:num_fixed_velocity_dumbbells] = DFUb_out[0:num_fixed_velocity_dumbbells]
            DFb_out[num_fixed_velocity_dumbbells:num_dumbbells] = DFb_in[num_fixed_velocity_dumbbells:num_dumbbells]
            HalfDUb_out[0:num_fixed_velocity_dumbbells] = HalfDUb_in[0:num_fixed_velocity_dumbbells]
            HalfDUb_out[num_fixed_velocity_dumbbells:num_dumbbells] = DFUb_out[num_fixed_velocity_dumbbells:num_dumbbells]
            if extract_force_on_wall_due_to_dumbbells:
                print("WARNING: Cannot extract force on wall due to dumbbells in UFTEU mode. Use UFTE mode instead.")

        elif input_form == 'duf':  # Dumbbells only, some imposed velocities
            num_fixed_velocity_dumbbells = num_dumbbells - Ub_in.count(['pippa', 'pippa', 'pippa'])
            try:
                force_vector = construct_force_vector_from_fts(
                    posdata, Fa_in, Ta_in, Ea_in,
                    Ub_in[0:num_fixed_velocity_dumbbells] + Fb_in[num_fixed_velocity_dumbbells:num_dumbbells],
                    HalfDUb_in[0:num_fixed_velocity_dumbbells] + DFb_in[num_fixed_velocity_dumbbells:num_dumbbells])
            except:
                throw_error("DUF mode has been selected but not enough values of U (dumbbell) and F (dumbbell) have been provided. At a guess, not all your dumbbells have either a U or an F.")
            force_vector = np.array(force_vector, float)
            grand_resistance_matrix_duf = fts_to_duf_matrix(num_fixed_velocity_dumbbells,
                                                            posdata, grand_resistance_matrix)
            velocity_vector = np.linalg.solve(grand_resistance_matrix_duf, force_vector)
            (Fa_out, Oa_out, Sa_out, FUb_out, DFUb_out) = deconstruct_velocity_vector_for_fts(posdata, velocity_vector)
            Fb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            Ub_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            DFb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            HalfDUb_out = [['chen', 'chen', 'chen'] for i in range(num_dumbbells)]
            Fb_out[0:num_fixed_velocity_dumbbells] = FUb_out[0:num_fixed_velocity_dumbbells]
            Fb_out[num_fixed_velocity_dumbbells:num_dumbbells] = Fb_in[num_fixed_velocity_dumbbells:num_dumbbells]
            Ub_out[0:num_fixed_velocity_dumbbells] = Ub_in[0:num_fixed_velocity_dumbbells]
            Ub_out[num_fixed_velocity_dumbbells:num_dumbbells] = FUb_out[num_fixed_velocity_dumbbells:num_dumbbells]
            DFb_out[0:num_fixed_velocity_dumbbells] = DFUb_out[0:num_fixed_velocity_dumbbells]
            DFb_out[num_fixed_velocity_dumbbells:num_dumbbells] = DFb_in[num_fixed_velocity_dumbbells:num_dumbbells]
            HalfDUb_out[0:num_fixed_velocity_dumbbells] = HalfDUb_in[0:num_fixed_velocity_dumbbells]
            HalfDUb_out[num_fixed_velocity_dumbbells:num_dumbbells] = DFUb_out[num_fixed_velocity_dumbbells:num_dumbbells]
            (Fa_out, Ta_out, Ea_out) = (Fa_in[:], Ta_in[:], Ea_in[:])
            Ua_out, Oa_out = np.array([]), np.array([])

        Fa_out = np.asarray(Fa_out, float)
        Ta_out = np.asarray(Ta_out, float)
        Ea_out = np.asarray(Ea_out, float)
        Ua_out = np.asarray(Ua_out, float)
        Oa_out = np.asarray(Oa_out, float)
        Ub_out = np.asarray(Ub_out, float)
        HalfDUb_out = np.asarray(HalfDUb_out, float)

    elapsed_solve_time = time.time() - solve_time_start
    gen_times.append(elapsed_solve_time)

    if (printout > 0):
        print("Velocities on particles 0-9")
        print(np.asarray(Ua_out[0:10]))
        print(np.asarray(Ub_out[0:10]))
        print("Half Delta U velocity 0-9")
        print(np.asarray(HalfDUb_out[0:10]))
        print("Omegas on particles 0-9")
        print(np.asarray(Oa_out[0:10]))
        print("Forces 0-9 (F)")
        print(np.asarray(Fa_out[0:10]))
        print(np.asarray(Fb_out[0:10]))
        print("Delta F forces 0-9 (DF)")
        print(np.asarray(DFb_out[0:10]))
        print("Torques 0-9 (T)")
        print(np.asarray(Ta_out[0:10]))
        print("Strain rate")
        print(np.asarray(Ea_out))
        print("Stresslets 0-9 (S)")
        print(np.asarray(Sa_out[0:10]))

    return (Fa_out, Ta_out, Sa_out, Fb_out, DFb_out,
            Ua_out, Oa_out, Ea_out, Ub_out, HalfDUb_out,
            last_generated_Minfinity_inverse, gen_times,
            U_infinity, O_infinity, centre_of_background_flow,
            Ot_infinity, Et_infinity,
            force_on_wall_due_to_dumbbells, last_velocity_vector)


def calculate_time_left(times, frameno, num_frames, invert_m_every,
                        checkpoint_start_from_frame):
    """Calculate time left in simulation, in seconds.

    Args:
        times: List of times for each timestep so far.
        frameno: Current timestep/frame number.
        num_frames: Total number of timesteps/frames.
        invert_m_every: How often M^infinity is being inverted (every n frames).
        checkpoint_start_from_frame: Frame no. at start of simulation.

    Returns:
        timeleft: Estimated remaining time, in seconds.
        flags (str): A string with characters suggesting missing information.
    """

    longtimes = [times[i] for i in range(checkpoint_start_from_frame, frameno + 1)
                 if i % invert_m_every == 0 or i == checkpoint_start_from_frame]

    # If Numba is on, first timestep is extra long because of compilation,
    # so should be ignored ASAP
    numba_compilation_time_not_discounted_flag = ""
    if not numba.config.DISABLE_JIT:
        if len(longtimes) >= 2:
            longtimeaverage = sum(longtimes[1:]) / len(longtimes[1:])
        elif len(longtimes) == 1:
            numba_compilation_time_not_discounted_flag = "<"
            longtimeaverage = longtimes[0]
        else:
            longtimeaverage = 0
    else:
        if len(longtimes) > 0:
            longtimeaverage = sum(longtimes) / len(longtimes)
        else:
            longtimeaverage = 0

    shorttimes = [times[i] for i in range(checkpoint_start_from_frame, frameno + 1)
                  if i % invert_m_every != 0 and i != checkpoint_start_from_frame]
    if len(shorttimes) != 0:
        shorttimeaverage = sum(shorttimes) / len(shorttimes)
    else:
        shorttimeaverage = longtimeaverage
    numberoflongtimesleft = len([i for i in range(frameno + 1, num_frames)
                                 if i % invert_m_every == 0])
    numberofshorttimesleft = len([i for i in range(frameno + 1, num_frames)
                                  if i % invert_m_every != 0])

    timeleft = (numberofshorttimesleft * shorttimeaverage
                + numberoflongtimesleft * longtimeaverage) * 1.03

    # The 1.03 is to sort of allow for the things this isn't counting.
    # On average it appears to be a good guess.

    if frameno - checkpoint_start_from_frame == 0 and numberofshorttimesleft > 0:
        no_short_times_yet_flag = "<"
    else:
        no_short_times_yet_flag = ""

    flags = (numba_compilation_time_not_discounted_flag
             + no_short_times_yet_flag)

    return (timeleft, flags)


def format_time_left(timeleft, flags):
    """Return string containing formatted time remaining."""
    if timeleft > 86400:  # 24 hours
        start_color = "\033[94m"
    elif timeleft > 18000:  # 5 hours
        start_color = "\033[95m"
    elif timeleft > 3600:  # 1 hour
        start_color = "\033[91m"
    elif timeleft > 600:  # 10 mins
        start_color = "\033[93m"
    else:
        start_color = "\033[92m"
    end_color = "\033[0m"

    return (start_color
            + flags
            + format_elapsed_time(timeleft)
            + end_color)


def format_finish_time(timeleft, flags):
    """Return string containing formatted finish time of simulation."""
    now = datetime.datetime.now()
    finishtime = now + datetime.timedelta(0, timeleft)
    if finishtime.date() == now.date():
        return flags + finishtime.strftime("%H:%M")
    elif (finishtime.date() - now.date()).days < 7:
        return flags + finishtime.strftime("%a %H:%M")
    else:
        return flags + finishtime.strftime("%d/%m/%y %H:%M")


def wrap_around(new_sphere_positions, box_bottom_left, box_top_right,
                Ot_infinity=np.array([0, 0, 0]),
                Et_infinity=np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]])):
    """Return sphere positions modulo the periodic box.

    This ideally should just be
        new_sphere_positions = 
            np.mod(new_sphere_positions + np.array([Lx/2.,Ly/2.,Lz/2.]),Lx)
                - np.array([Lx/2.,Ly/2.,Lz/2.])
    but if you have a slanted box /_/ and you go off the top, you actually
    want to go to the bottom and slightly to the left.
    This is achieved by instead doing
        new_sphere_positions = 
            SHEAR [ np.mod( UNSHEAR [ new_sphere_positions ] + np.array([Lx/2.,Ly/2.,Lz/2.]),Lx) 
                   - np.array([Lx/2.,Ly/2.,Lz/2.]) ]
    """
    box_dimensions = box_top_right - box_bottom_left
    # Then shear the basis vectors
    basis_canonical = np.diag(box_dimensions)  # which equals np.array([[Lx,0,0],[0,Ly,0],[0,0,Lz]])
    sheared_basis_vectors = shear_basis_vectors(
        basis_canonical, box_dimensions, Ot_infinity, Et_infinity)
    # Hence
    new_sphere_positions = np.dot(np.mod(np.dot(new_sphere_positions,
                                                np.linalg.inv(sheared_basis_vectors)) + 0.5,
                                         [1, 1, 1]) - 0.5, sheared_basis_vectors)
    return new_sphere_positions


def add_background_flow_spheres(Ua_out_k1, Oa_out_k1, Ea_out_k1,
                                U_infinity_k1, O_infinity_k1,
                                sphere_positions, centre_of_background_flow):
    """Return Ua_out and Oa_out + the background flow."""
    num_spheres = sphere_positions.shape[0]
    O_infinity_cross_x_k1 = np.cross(O_infinity_k1,
                                     sphere_positions - centre_of_background_flow)
    E_infinity_dot_x_k1 = np.empty([num_spheres, 3])
    for i in range(num_spheres):
        E_infinity_dot_x_k1[i] = np.dot(Ea_out_k1[i],
                                        sphere_positions[i] - centre_of_background_flow)

    Ua_out_plus_infinities_k1 = (Ua_out_k1
                                 + U_infinity_k1
                                 + O_infinity_cross_x_k1
                                 + E_infinity_dot_x_k1)
    Oa_out_plus_infinities_k1 = Oa_out_k1 + O_infinity_k1

    return Ua_out_plus_infinities_k1, Oa_out_plus_infinities_k1


def add_background_flow_dumbbells(Ub_out_k1, HalfDUb_out_k1, Ea_out_k1,
                                  U_infinity_k1, O_infinity_k1,
                                  dumbbell_positions, dumbbell_deltax,
                                  centre_of_background_flow):
    """Return Ub_out and HalfDUb_out + the background flow."""
    num_dumbbells = dumbbell_positions.shape[0]
    O_infinity_cross_xbar_k1 = np.cross(O_infinity_k1,
                                        dumbbell_positions - centre_of_background_flow)
    O_infinity_cross_deltax_k1 = np.cross(O_infinity_k1, dumbbell_deltax)
    E_infinity_dot_xbar_k1 = np.empty([num_dumbbells, 3])
    E_infinity_dot_deltax_k1 = np.empty([num_dumbbells, 3])
    for i in range(num_dumbbells):
        E_infinity_dot_xbar_k1[i] = np.dot(Ea_out_k1[0],
                                           dumbbell_positions[i] - centre_of_background_flow)
        E_infinity_dot_deltax_k1[i] = np.dot(Ea_out_k1[0], dumbbell_deltax[i])

    Ub_out_plus_infinities_k1 = (Ub_out_k1 + U_infinity_k1
                                 + O_infinity_cross_xbar_k1
                                 + E_infinity_dot_xbar_k1)
    HalfDUb_out_plus_infinities_k1 = (HalfDUb_out_k1
                                      + 0.5*(O_infinity_cross_deltax_k1
                                             + E_infinity_dot_deltax_k1))

    return Ub_out_plus_infinities_k1, HalfDUb_out_plus_infinities_k1
